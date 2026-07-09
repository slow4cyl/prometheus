#!/usr/bin/env python3
"""One-shot: backfill task_prior_feed stamps for the pre-stamp-era shelf.

Why: the independence gate (independence_gate.py) was armed but toothless —
it applies 0 haircuts because the durable prior_fed stamp (task_prior_feed,
in prometheus.db) only exists since 2026-07-04, while the discovery shelf's
supports predate it. The "unknown-body" premise it assumed is stale: kanban
no longer nulls task bodies, so ~99.6% of tasks retain a recoverable body and
prior_fed can be derived from it (FEED_MARK presence) — the exact signal the
gate's own arming audit validated at 99.99% agreement with creation stamps.

This stamps every kanban task (live tasks + archived_tasks) that has a body,
a t_ id, is older than the settle window, and has no task_prior_feed row yet.
prior_fed is body-derived with the SAME rule as prior_feed_stamp.record().
INSERT OR IGNORE — a real creation stamp is never overwritten. Every inserted
id is recorded in task_prior_feed_backfill for exact rollback.

    python3 backfill_prior_feed_stamps.py            # dry-run: report only
    python3 backfill_prior_feed_stamps.py --apply     # do it
    python3 backfill_prior_feed_stamps.py --rollback   # undo (sidecar DELETE)
"""
import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prior_feed_stamp import FEED_MARK, _fed_hashes  # single source of truth

HERMES = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
PROM = os.path.join(HERMES, "prometheus.db")
KANBAN = os.path.join(HERMES, "kanban.db")
SETTLE_SECONDS = 15 * 60
BATCH = 5000


def _candidates():
    """Yield (task_id, body) for every unstamped, bodied, settled t_ task."""
    p = sqlite3.connect(f"file:{PROM}?mode=ro", uri=True)
    stamped = {r[0] for r in p.execute("SELECT kanban_task_id FROM task_prior_feed")}
    p.close()
    k = sqlite3.connect(f"file:{KANBAN}?mode=ro", uri=True)
    cutoff = time.time() - SETTLE_SECONDS
    for table in ("tasks", "archived_tasks"):
        try:
            cur = k.execute(
                f"SELECT id, body, created_at FROM {table} "
                "WHERE id LIKE 't\\_%' ESCAPE '\\' AND body IS NOT NULL AND TRIM(body) != ''")
        except sqlite3.OperationalError:
            continue
        for tid, body, created in cur:
            if tid in stamped:
                continue
            try:
                if created and float(created) > cutoff:
                    continue
            except (TypeError, ValueError):
                pass
            yield tid, body
    k.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    if args.rollback:
        conn = sqlite3.connect(PROM, timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            ts = conn.execute(
                "SELECT backfill_ts FROM task_prior_feed_backfill LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            print("no backfill sidecar — nothing to roll back")
            return
        if not ts:
            print("sidecar empty — nothing to roll back")
            return
        # Backfilled rows all share created_at == backfill_ts (a microsecond
        # float); a real creation stamp never collides, so this deletes exactly
        # what the backfill inserted and nothing else.
        cur = conn.execute("DELETE FROM task_prior_feed WHERE created_at = ?", (ts[0],))
        conn.execute("DROP TABLE task_prior_feed_backfill")
        conn.commit()
        conn.close()
        print(f"rolled back {cur.rowcount} backfilled stamps; sidecar dropped")
        return

    total = fed = blind = 0
    rows = []
    for tid, body in _candidates():
        is_fed = FEED_MARK in (body or "")
        total += 1
        fed += is_fed
        blind += (not is_fed)
        rows.append((tid, 1 if is_fed else 0,
                     len(_fed_hashes(body)) if is_fed else 0,
                     json.dumps(_fed_hashes(body)) if is_fed else "[]"))

    print(f"stampable tasks: {total}  (prior_fed={fed}, blind={blind})")
    if not args.apply:
        print("DRY-RUN — no writes. Re-run with --apply to stamp.")
        return

    conn = sqlite3.connect(PROM, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    now = time.time()
    conn.execute("DROP TABLE IF EXISTS task_prior_feed_backfill")
    conn.execute("CREATE TABLE task_prior_feed_backfill (backfill_ts REAL NOT NULL)")
    conn.execute("INSERT INTO task_prior_feed_backfill (backfill_ts) VALUES (?)", (now,))
    conn.commit()
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        conn.executemany(
            "INSERT OR IGNORE INTO task_prior_feed "
            "(kanban_task_id, prior_fed, n_fed, fed_hashes, created_at) VALUES (?,?,?,?,?)",
            [(tid, pf, nf, fh, now) for (tid, pf, nf, fh) in chunk])
        conn.commit()
    stamped_now = conn.execute(
        "SELECT COUNT(*) FROM task_prior_feed WHERE created_at = ?", (now,)).fetchone()[0]
    conn.close()
    print(f"stamped {stamped_now} tasks (rollback: this script --rollback)")


if __name__ == "__main__":
    main()
