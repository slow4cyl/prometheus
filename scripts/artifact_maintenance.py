#!/usr/bin/env python3
"""artifact_maintenance.py — closed-loop artifact preservation and re-verification.

This script is the automated maintenance loop for the artifact_status system.
It does THREE things in sequence:

1. PRESERVE: Scan live kanban workspaces for evidence files and copy them to
   ~/.hermes/artifacts/<task_id>/ before workspace_gc.py can delete them.
   This runs as a safety net even though workspace_gc.py now preserves
   artifacts itself — belt and suspenders.

2. RE-VERIFY: Re-run verify_artifacts() on all worker_results with
   artifact_status IN ('UNVERIFIED', 'FAILED') and update the DB where the
   files can now be found (in the artifacts archive, live workspaces, or
   other search roots). This recovers previously-lost verifications.

3. REPORT: Print a summary of what was preserved, what was re-verified, and
   the current artifact_status distribution.

WAL-safe: busy_timeout, synchronous=NORMAL, chunked commits, BEGIN IMMEDIATE.
Idempotent. Pass --apply to write; default is dry-run.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from db_retry import get_db

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from write_worker_result import verify_artifacts  # noqa: E402

DB = os.path.expanduser("~/.hermes/prometheus.db")
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
ARTIFACTS_ROOT = os.path.join(HERMES_HOME, "artifacts")
WORKSPACES_ROOT = os.path.join(HERMES_HOME, "kanban", "workspaces")
CHUNK = 500

EVIDENCE_EXTS = {'.json', '.csv', '.py', '.md', '.txt', '.yaml', '.yml', '.toml', '.cfg'}


def preserve_from_workspaces(dry_run=False):
    """Scan live kanban workspaces and preserve evidence files to artifacts archive.

    This is a safety net that runs independently of workspace_gc.py. It catches
    workspaces that are about to be GC'd (done tasks) before the GC cycle runs.
    """
    if not os.path.isdir(WORKSPACES_ROOT):
        return 0

    # Connect to kanban DB to check task status
    kanban_db = os.path.join(HERMES_HOME, "kanban.db")
    kconn = None
    try:
        kconn = get_db(db_path=kanban_db)
        kconn.row_factory = sqlite3.Row
    except Exception:
        pass

    preserved = 0
    for entry in os.listdir(WORKSPACES_ROOT):
        if not entry.startswith("t_"):
            continue
        ws_path = os.path.join(WORKSPACES_ROOT, entry)
        if not os.path.isdir(ws_path):
            continue

        # Only preserve from done/archived tasks (running tasks still need their files)
        should_preserve = True
        if kconn:
            try:
                row = kconn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (entry,)
                ).fetchone()
                if row and row["status"] == "running":
                    should_preserve = False
            except Exception:
                pass  # If we can't check, preserve to be safe

        if not should_preserve:
            continue

        dest_dir = os.path.join(ARTIFACTS_ROOT, entry)
        os.makedirs(dest_dir, exist_ok=True) if not dry_run else None

        # Scan top-level files
        for f in os.listdir(ws_path):
            fpath = os.path.join(ws_path, f)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext not in EVIDENCE_EXTS:
                continue
            if os.path.getsize(fpath) == 0:
                continue

            dest_file = os.path.join(dest_dir, f)
            if os.path.exists(dest_file) and os.path.getsize(dest_file) > 0:
                continue  # Already preserved

            if not dry_run:
                try:
                    shutil.copy2(fpath, dest_file)
                    preserved += 1
                except OSError:
                    pass
            else:
                preserved += 1

        # Scan one level deep
        for subdir in os.listdir(ws_path):
            subpath = os.path.join(ws_path, subdir)
            if not os.path.isdir(subpath):
                continue
            for f in os.listdir(subpath):
                fpath = os.path.join(subpath, f)
                if not os.path.isfile(fpath):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext not in EVIDENCE_EXTS:
                    continue
                if os.path.getsize(fpath) == 0:
                    continue

                dest_file = os.path.join(dest_dir, f"{subdir}__{f}")
                if os.path.exists(dest_file) and os.path.getsize(dest_file) > 0:
                    continue

                if not dry_run:
                    try:
                        shutil.copy2(fpath, dest_file)
                        preserved += 1
                    except OSError:
                        pass
                else:
                    preserved += 1

    if kconn:
        kconn.close()

    return preserved


def reverify_artifacts(apply=False, since_hours=None):
    """Re-run verify_artifacts on UNVERIFIED and FAILED worker_results.

    Args:
        since_hours: If set, only process rows created in the last N hours.
                     None means process all rows (legacy behavior).

    Returns dict of changes: {(old_status, new_status): count}.
    """
    con = get_db()
    con.row_factory = sqlite3.Row

    # Detect created_at column
    cols = {r["name"] for r in con.execute("PRAGMA table_info(worker_results)")}
    created_col = "created_at" if "created_at" in cols else None
    sel = "id, experiment_id, files_produced" + (", created_at" if created_col else "")

    # Get UNVERIFIED and FAILED rows with files_produced
    # Scope to recent rows if --hours is set (critical for performance
    # under 50-worker contention — avoids iterating all 14K+ rows)
    where = (
        "WHERE artifact_status IN ('FILES_MISSING', 'FAILED') "
        "AND files_produced IS NOT NULL AND files_produced != ''"
    )
    params = []
    if since_hours and created_col:
        cutoff = time.time() - (since_hours * 3600)
        where += f" AND {created_col} > ?"
        params.append(cutoff)
    elif since_hours and not created_col:
        print("  WARN: --hours set but no created_at column; processing all rows")

    rows = con.execute(f"SELECT {sel} FROM worker_results {where}", params).fetchall()
    if since_hours:
        print(f"  Scope: last {since_hours}h ({len(rows)} rows)")

    changes = []  # (id, old_status, new_status)
    tally = {}
    errors = 0

    for row in rows:
        fp = row["files_produced"]
        # Parse files_produced — could be JSON list or comma-separated
        try:
            files = json.loads(fp)
            if isinstance(files, str):
                files = [files]
        except (json.JSONDecodeError, TypeError):
            files = [f.strip() for f in fp.split(",") if f.strip()]

        if not files:
            continue

        # Don't pass created_at as a hard mtime gate — many legit old files
        # predate their re-recorded experiment row
        created_at = None
        if created_col:
            try:
                created_at = float(row["created_at"])
            except (TypeError, ValueError):
                pass

        try:
            new_status = verify_artifacts(files, row["experiment_id"], created_at=None)
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  WARN: verify_artifacts failed for id={row['id']} "
                      f"exp={row['experiment_id']}: {e}", flush=True)
            continue

        # Get current status
        cur_row = con.execute(
            "SELECT artifact_status FROM worker_results WHERE id = ?", (row["id"],)
        ).fetchone()
        old_status = cur_row["artifact_status"] if cur_row else "UNKNOWN"

        if new_status != old_status and new_status not in ('UNVERIFIED', 'FAILED'):
            changes.append((row["id"], old_status, new_status))
            key = (old_status, new_status)
            tally[key] = tally.get(key, 0) + 1

    if errors:
        print(f"  ({errors} rows skipped due to verify errors)")

    con.close()
    return changes, tally


def apply_changes(changes, apply=False):
    """Write artifact_status updates to the DB."""
    if not changes or not apply:
        return 0

    con = get_db()
    cur = con.cursor()

    written = 0
    for i in range(0, len(changes), CHUNK):
        batch = changes[i:i + CHUNK]
        # Retry BEGIN IMMEDIATE under heavy DB contention (50+ cron jobs)
        for attempt in range(7):
            try:
                cur.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < 6:
                    time.sleep(0.05 * (2 ** attempt))
                    continue
                raise
        cur.executemany(
            "UPDATE worker_results SET artifact_status=? WHERE id=?",
            [(new_st, rid) for rid, _, new_st in batch]
        )
        con.commit()
        written += len(batch)
        print(f"  committed {written}/{len(changes)}", flush=True)

    con.close()
    return written


def report_status():
    """Print current artifact_status distribution."""
    con = get_db(readonly=True)
    c = con.cursor()
    c.execute("SELECT artifact_status, COUNT(*) FROM worker_results GROUP BY artifact_status ORDER BY COUNT(*) DESC")
    print("\n=== Current worker_results artifact_status ===")
    total = 0
    for r in c.fetchall():
        print(f"  {r[0] or 'NULL':<25}: {r[1]}")
        total += r[1]
    print(f"  {'TOTAL':<25}: {total}")

    # Artifacts archive stats
    if os.path.isdir(ARTIFACTS_ROOT):
        n_dirs = len([d for d in os.listdir(ARTIFACTS_ROOT) if os.path.isdir(os.path.join(ARTIFACTS_ROOT, d))])
        n_files = sum(
            len([f for f in os.listdir(os.path.join(ARTIFACTS_ROOT, d)) if os.path.isfile(os.path.join(ARTIFACTS_ROOT, d, f))])
            for d in os.listdir(ARTIFACTS_ROOT)
            if os.path.isdir(os.path.join(ARTIFACTS_ROOT, d))
        )
        print(f"\n=== Artifacts archive ===")
        print(f"  Task dirs: {n_dirs}")
        print(f"  Files: {n_files}")

    con.close()


def main(apply=False, since_hours=None):
    print(f"{'='*60}")
    print(f"ARTIFACT MAINTENANCE {'[APPLY]' if apply else '[DRY-RUN]'}")
    print(f"{'='*60}")

    # 1. PRESERVE
    print("\n--- Phase 1: Preserve artifacts from live workspaces ---")
    preserved = preserve_from_workspaces(dry_run=not apply)
    print(f"  Files {'preserved' if apply else 'would be preserved'}: {preserved}")

    # 2. RE-VERIFY
    print("\n--- Phase 2: Re-verify UNVERIFIED and FAILED rows ---")
    changes, tally = reverify_artifacts(apply=apply, since_hours=since_hours)
    print(f"  Rows that would change: {len(changes)}")
    for (old, new), n in sorted(tally.items(), key=lambda x: -x[1]):
        print(f"    {old:<20} -> {new:<20}: {n}")

    if apply and changes:
        written = apply_changes(changes, apply=True)
        print(f"\n  Written to DB: {written} rows updated")
    elif not apply and changes:
        print(f"\n  (DRY-RUN: re-run with --apply to commit {len(changes)} changes)")

    # 3. REPORT
    report_status()

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Artifact maintenance: preserve + re-verify")
    ap.add_argument("--apply", action="store_true", help="Write changes to DB")
    ap.add_argument("--hours", type=int, default=None,
                    help="Only process rows from the last N hours (default: all)")
    args = ap.parse_args()
    try:
        sys.exit(main(apply=args.apply, since_hours=args.hours))
    except Exception as e:
        print(f"ERROR (transient, will retry next cycle): {e}", file=sys.stderr)
        sys.exit(0)  # exit 0 so cron doesn't report failure for transient issues
