#!/usr/bin/env python3
"""tmp_stray_janitor.py — keep /tmp, $HOME, and the .hermes root free of
worker/infrastructure debris.

Why: three mechanical leaks filled a 24 GB tmpfs and littered the tree —
gpu_sklearn handoff dirs (fixed at source in gpu_sklearn/_core.py), browser
profiles + shell snapshot stubs (rerouted via TMPDIR=workspace at worker
spawn), and workers writing hardcoded absolute paths outside their workspace
(behavioral — cannot be fully fixed by prompting stateless workers). This
janitor handles the legacy backlog and any future stragglers.

Policy — deletion only in /tmp, move-never-delete everywhere else:
  /tmp (DELETE, age-gated):
    gpu_sklearn_*                 > 2h   (scratch; fixed at source, this is backlog)
    agent-browser-* / org.chromium.* / .org.chromium.*  > 2h  (browser scratch)
    hermes-snap-*.sh / hermes-cwd-*.txt                 > 24h (shell session stubs)
  ~/.hermes ROOT and $HOME ROOT (MOVE to ~/.hermes/archive/stray-sweeps/<date>/):
    files (not dirs) matching worker-output patterns, age > 7 days:
      exp_*.py exp_*.json exp_*.csv exp_*.txt exp_*.md tmp_exp_* write_bench3_*
  0-byte GHOST DBs (MOVE, age > 2h), in root + the cwd-accident dirs (data/ prometheus/
    kanban/ prometheus_worker_results/ …): empty *.db/*.sqlite files an agent minted by
    running `sqlite3 <bare-name>` / connect('<name>.db') from the HERMES cwd — sqlite
    auto-creates the file, so a guessed/typo'd name (knowledge_clames.db, self_state.db,
    prometheus_db=prometheus.db) leaves an empty ghost. Behavioral (LLM guesses names,
    can't be prompted away); 0-byte ⇒ no data to lose; a live DB is never 0-byte.
  Deliberately untouched: ~/.hermes/experiments/ and scripts/ (legit persistent
  roots that verify_artifacts/find_archived_code search), workspaces, artifacts.

Runs silent when nothing to do. --dry-run prints the plan without acting.
"""
import argparse
import os
import shutil
import time

HOME = os.path.expanduser("~")
HERMES = os.path.join(HOME, ".hermes")
SWEEP_ROOT = os.path.join(HERMES, "archive", "stray-sweeps")

TMP_DELETE = [
    # (glob-ish prefix match, suffix match or None, min age seconds)
    ("gpu_sklearn_", None, 2 * 3600),
    ("agent-browser-", None, 2 * 3600),
    ("org.chromium.", None, 2 * 3600),
    (".org.chromium.", None, 2 * 3600),
    ("hermes-snap-", ".sh", 24 * 3600),
    ("hermes-cwd-", ".txt", 24 * 3600),
]

STRAY_PREFIXES = ("exp_", "tmp_exp_", "tmp_", "write_bench3_",
                  "verify-", "verify_", "hermes-verify-", "backfill_plan_")
STRAY_EXTS = (".py", ".json", ".csv", ".txt", ".md", ".png", ".npy")
STRAY_MIN_AGE = 2 * 86400   # 2d (was 7d): root exp_/tmp_/verify strays are worker
                            # cwd-accidents (the loop writes to workspaces, not root).
                            # Generic-named strays (results.json, analysis.json) are left
                            # to the referenced-aware one-shot, not this blunt prefix match.

# --- 0-byte ghost DB sweep (cwd-relative sqlite strays) ---
DB_EXTS = (".db", ".sqlite", ".sqlite3")
GHOST_DB_MIN_AGE = 2 * 3600  # 2h: a real DB is 0-byte only in the instant before its
                             # first schema write; the creating process is long gone.
# Live DBs are NEVER swept (basename match — redundant with the 0-byte check since a
# live DB always has data, but kept as defense-in-depth).
LIVE_DBS = frozenset((
    "prometheus.db", "state.db", "lcm.db", "kanban.db", "verification_evidence.db",
    "gpu_scheduler.db", "projects.db", "changelog.db", "gpu_queue.db", "experiments.db", "rag.db",
))
# Dirs where cwd-relative connect() strays land: HERMES root (agents run from here) +
# the recurring cwd-accident subdirs. NOT scripts/experiments (protected code roots).
GHOST_DB_DIRS = (HERMES, HOME) + tuple(os.path.join(HERMES, d) for d in (
    "data", "prometheus", "kanban", "prometheus_worker_results", "director-workspace"))


def sweep_tmp(dry_run):
    deleted = freed = 0
    now = time.time()
    try:
        entries = os.listdir("/tmp")
    except OSError:
        return 0, 0
    for name in entries:
        for prefix, suffix, min_age in TMP_DELETE:
            if not name.startswith(prefix):
                continue
            if suffix and not name.endswith(suffix):
                continue
            path = os.path.join("/tmp", name)
            try:
                st = os.lstat(path)
            except OSError:
                break
            if st.st_uid != os.getuid() or now - st.st_mtime < min_age:
                break
            size = 0
            if os.path.isdir(path) and not os.path.islink(path):
                for root, _, files in os.walk(path, onerror=lambda e: None):
                    for f in files:
                        try:
                            size += os.lstat(os.path.join(root, f)).st_size
                        except OSError:
                            pass
                if not dry_run:
                    shutil.rmtree(path, ignore_errors=True)
            else:
                size = st.st_size
                if not dry_run:
                    try:
                        os.unlink(path)
                    except OSError:
                        break
            deleted += 1
            freed += size
            break
    return deleted, freed


def is_stray(name):
    return (name.startswith(STRAY_PREFIXES)
            and name.endswith(STRAY_EXTS))


def sweep_strays(root, dry_run, moved_log):
    """Move aged worker-output files from a directory ROOT (non-recursive)."""
    moved = 0
    now = time.time()
    dest_dir = os.path.join(SWEEP_ROOT, time.strftime("%Y%m%d"))
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        if not is_stray(name):
            continue
        path = os.path.join(root, name)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if now - st.st_mtime < STRAY_MIN_AGE:
            continue
        dest = os.path.join(dest_dir, name)
        # collision-safe: prefix with source dir marker
        if os.path.exists(dest):
            marker = "home" if root == HOME else "hermes"
            dest = os.path.join(dest_dir, f"{marker}__{name}")
        if not dry_run:
            os.makedirs(dest_dir, exist_ok=True)
            try:
                shutil.move(path, dest)
            except (OSError, shutil.Error):
                continue
        moved_log.append(f"{path} -> {dest}")
        moved += 1
    return moved


def sweep_ghost_dbs(dry_run, moved_log):
    """Move 0-byte ghost DBs + SQL-query-as-filename junk from root + cwd-accident dirs.
    Empty *.db an agent left by connecting to a bare/guessed name; 0-byte ⇒ no data."""
    moved = 0
    now = time.time()
    dest_dir = os.path.join(SWEEP_ROOT, time.strftime("%Y%m%d"))
    for root in GHOST_DB_DIRS:
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for name in entries:
            is_db = name.endswith(DB_EXTS) and name not in LIVE_DBS
            is_junk = name.startswith("SELECT ") or name.startswith("prometheus_db=")
            if not (is_db or is_junk):
                continue
            path = os.path.join(root, name)
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if st.st_uid != os.getuid():
                continue
            if is_db and st.st_size != 0:          # a populated DB is not a ghost — never touch
                continue
            if now - st.st_mtime < GHOST_DB_MIN_AGE:
                continue
            marker = "hermes" if root == HERMES else os.path.basename(root)
            dest = os.path.join(dest_dir, f"{marker}__{name}")
            if not dry_run:
                os.makedirs(dest_dir, exist_ok=True)
                try:
                    shutil.move(path, dest)
                except (OSError, shutil.Error):
                    continue
            moved_log.append(f"{path} -> {dest}")
            moved += 1
    return moved


def main():
    ap = argparse.ArgumentParser(description="Sweep worker/infrastructure debris")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    deleted, freed = sweep_tmp(args.dry_run)
    moved_log = []
    moved = sweep_strays(HERMES, args.dry_run, moved_log)
    moved += sweep_strays(HOME, args.dry_run, moved_log)
    moved += sweep_ghost_dbs(args.dry_run, moved_log)

    mode = "[DRY-RUN] " if args.dry_run else ""
    if deleted or moved:
        print(f"{mode}/tmp: {deleted} entries deleted ({freed / 1024 / 1024:.0f} MB); "
              f"strays moved to archive/stray-sweeps/: {moved}")
        for line in moved_log[:15]:
            print(f"  {line}")
        if len(moved_log) > 15:
            print(f"  ... and {len(moved_log) - 15} more")
    # silent when nothing to do (cron-friendly)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
