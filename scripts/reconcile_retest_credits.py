#!/usr/bin/env python3
"""reconcile_retest_credits.py — periodic safety net for retest credit.

apply_worker_results block 1g credits a completed [CANDIDATE-RETEST] the moment
its result lands, but its FIRST step (_extract_curiosity_id_from_task) opens a
fresh kanban connection per result and swallows any lock/timeout to None — so
under concurrent load (≈20 workers + crons on kanban.db) roughly HALF of retest
credits silently vanish with no WARN. Measured lifetime yield: 52 credited of
157 completed retests (36%); 92 recoverable. Since n_independent_retests gates
2,036 high-wsc claims out of REPLICATED, that lost half throttles the whole
ladder.

This reconciler does the same credit as 1g but ROBUSTLY: one batched kanban
read (single connection) of every completed retest task, joined to
worker_results, writing the replication_results row (and resolving the
curiosity) for any completed retest whose distinct experiment has no credit
yet. 1g stays the fast path; this is the guarantee that catches its misses.

Credit rule (mirrors 1g exactly):
  - task body carries CURIOSITY_ID: N; curiosity N has provenance
    candidate_retest/retest_gate and a source_experiment
  - a worker_result exists for the task with a distinct experiment_id
    (!= source_experiment)
  - verdict derivable and != REFUTED_SETUP  (SUPPORTED->replicated, else
    disagreed; text parse first, hypothesis_supported flag fallback)
  - no replication_results row for that experiment yet
Writes selection_reason='candidate_retest_reconciled' so its contribution is
distinguishable from the intake fast path.

Usage:
    python3 reconcile_retest_credits.py [--dry-run] [--window-hours N]
Runs live (applies) by default, matching the enqueuer cron convention.
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
TAG = "candidate_retest_reconciled"


def derive_verdict(text, flag):
    """Same normalization family as intake block 1g: leading verdict token in
    the finding text wins; fall back to the hypothesis_supported flag."""
    if text:
        m = re.match(
            r'^\s*(?:exp_\S+\s*:?\s*)?'
            r'(PARTIALLY\s+REFUTED|REFUTED_SETUP|REFUTED|'
            r'PARTIALLY[\s_-]+(?:CONFIRMED|SUPPORTED)|PARTIAL\s+SUPPORT(?:ED)?|'
            r'CONFIRMED|SUPPORTED|HYPOTHESIS\s+(?:CONFIRMED|SUPPORTED|REFUTED))',
            text, re.IGNORECASE)
        if m:
            raw = m.group(1).upper()
            if "REFUTED_SETUP" in raw:
                return "REFUTED_SETUP"
            if "REFUTED" in raw:
                return "REFUTED"
            return "SUPPORTED"
    if flag is not None:
        return "SUPPORTED" if flag else "REFUTED"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--window-hours", type=int, default=0,
                    help="only tasks completed within N hours (0 = all)")
    args = ap.parse_args()

    conn = get_db()                                  # prometheus (write)
    kconn = get_db(KANBAN_DB)                         # kanban (read) — ONE connection

    # 1. Batched read of every completed retest task + its body (single query).
    where_time = ""
    params = []
    if args.window_hours:
        where_time = "AND completed_at > strftime('%s','now') - ?"
        params.append(args.window_hours * 3600)
    ktasks = kconn.execute(f"""
        SELECT id, body FROM tasks
        WHERE (title LIKE '%CANDIDATE-RETEST%' OR title LIKE '%RETEST-GATE%')
          AND status IN ('done', 'archived', 'completed')
          {where_time}""", params).fetchall()
    kconn.close()

    # task_id -> curiosity_id from the body marker
    task_cur = {}
    for row in ktasks:
        m = re.search(r'CURIOSITY_ID:\s*(\d+)', row[1] or '')
        if m:
            task_cur[row[0]] = int(m.group(1))
    if not task_cur:
        print("No completed retest tasks with a CURIOSITY_ID marker.")
        conn.close()
        return 0

    # 2. Batch-load the curiosities.
    cur_ids = sorted(set(task_cur.values()))
    cur_info = {}
    for i in range(0, len(cur_ids), 800):
        chunk = cur_ids[i:i + 800]
        q = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT id, provenance, source_experiment, status, created_at "
                f"FROM curiosities WHERE id IN ({q})", chunk):
            cur_info[r[0]] = {"provenance": r[1], "source": r[2],
                              "status": r[3], "created_at": r[4]}

    credited = resolved = skipped = 0
    now = time.time()
    n_rep = n_dis = 0

    for task_id, cid in task_cur.items():
        ci = cur_info.get(cid)
        if not ci or ci["provenance"] not in ("candidate_retest", "retest_gate") \
                or not ci["source"]:
            continue
        wr = conn.execute(
            "SELECT experiment_id, key_finding, hypothesis_supported, confidence "
            "FROM worker_results WHERE kanban_task_id = ? "
            "AND experiment_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (task_id,)).fetchone()
        if not wr:
            continue
        exp_id, key_finding, hyp_supported, confidence = wr
        if exp_id == ci["source"]:
            continue

        # replication_results has UNIQUE(original_experiment_id): a claim's
        # source experiment gets exactly one credit row. If it already has one
        # (from 1g, the earlier backfill, or another retest of the same claim),
        # the claim is already credited — this retest is redundant-but-done, so
        # just resolve its curiosity (a source-guarded resolver never would).
        already = conn.execute(
            "SELECT 1 FROM replication_results "
            "WHERE original_experiment_id = ? LIMIT 1", (ci["source"],)).fetchone()

        verdict = derive_verdict(key_finding, hyp_supported)
        if not already and (verdict is None or verdict == "REFUTED_SETUP"):
            skipped += 1
            continue

        if args.dry_run:
            if not already:
                status = "replicated" if verdict == "SUPPORTED" else "disagreed"
                credited += 1
                n_rep += status == "replicated"
                n_dis += status == "disagreed"
            continue

        if not already:
            status = "replicated" if verdict == "SUPPORTED" else "disagreed"
            orig = conn.execute(
                "SELECT result, domain FROM experiments WHERE id = ?",
                (ci["source"],)).fetchone()
            # OR IGNORE: defense against a race with 1g on the same source.
            cur = conn.execute("""
                INSERT OR IGNORE INTO replication_results
                (original_experiment_id, original_finding, original_domain,
                 validation_task_id, validation_experiment_id, validation_finding,
                 validation_confidence, validation_hypothesis_supported,
                 replication_status, selected_at, validated_at, selection_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ci["source"], orig[0] if orig else None,
                 orig[1] if orig else None, task_id, exp_id,
                 (key_finding or "")[:2000],
                 float(confidence) if confidence is not None else None,
                 1 if verdict == "SUPPORTED" else 0, status,
                 ci["created_at"], now, TAG))
            if cur.rowcount:
                credited += 1
                n_rep += status == "replicated"
                n_dis += status == "disagreed"

        # Resolve the curiosity in either case (credit exists for the claim).
        if ci["status"] == "active":
            r = conn.execute(
                "UPDATE curiosities SET status='resolved', "
                "resolved_by_experiment=?, resolved_at=? WHERE id=? AND status='active'",
                (exp_id, now, cid))
            resolved += r.rowcount

    if not args.dry_run:
        conn.commit()

    # Sibling sweep: resolve any active retest curiosity whose SOURCE already
    # holds its credit — earned via a DIFFERENT curiosity's task. Block 1g
    # resolves only the curiosity its own task points at, and the task-walk
    # above only reaches curiosities that had a completed task; an
    # undispatched sibling stays active, clutters the queue slice, and mints
    # a wasted duplicate task if it reaches the refiller first (the refiller
    # now skips these too — this sweep clears them at the source).
    swept = 0
    if not args.dry_run:
        r = conn.execute("""
            UPDATE curiosities SET status='resolved',
                   resolved_by_experiment=(SELECT rr.validation_experiment_id
                                           FROM replication_results rr
                                           WHERE rr.original_experiment_id = curiosities.source_experiment),
                   resolved_at=?
            WHERE status='active'
              AND provenance IN ('candidate_retest', 'retest_gate')
              AND source_experiment IN (SELECT original_experiment_id FROM replication_results)""",
            (now,))
        swept = r.rowcount
        conn.commit()
    conn.close()

    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"[{mode}] reconciled {credited} retest credits "
          f"({n_rep} replicated, {n_dis} disagreed); "
          f"resolved {resolved} active curiosities; skipped {skipped} (setup/no-verdict); "
          f"swept {swept} moot siblings (source already credited)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
