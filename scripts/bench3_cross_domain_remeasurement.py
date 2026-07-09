#!/usr/bin/env python3
"""
bench3_cross_domain_remeasurement.py — Stratified BENCH3 accuracy: in-domain vs cross-domain.

Measures the accuracy gap between in-domain and cross-domain evidence on BENCH3
benchmark questions. The no-discount decision on cross-domain evidence was
confirmed at 4.9pp gap (2026-06-26). This script re-measures every 6h to detect
drift. Exits non-zero if gap exceeds 5pp threshold.

Writes ~/.hermes/bench3_cross_domain_status.json for watchdog monitoring.
Clean run = empty stdout (no_agent contract: silent when healthy).
Alert = prints summary to stdout for cron delivery.

Usage:
  python3 bench3_cross_domain_remeasurement.py          # normal run
  python3 bench3_cross_domain_remeasurement.py --quiet  # suppress stdout on alert
"""

import fcntl
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import sqlite3
import sys
import time

DB_PATH = _PP_PROMETHEUS_DB
STATUS_PATH = os.path.expanduser("~/.hermes/bench3_cross_domain_status.json")
LOCK_PATH = os.path.expanduser("~/.hermes/bench3_cross_domain.lock")
GAP_THRESHOLD = 5.0  # percentage points


def get_db():
    from db_retry import get_db
    return get_db(DB_PATH)


def acquire_lock():
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except IOError:
        print("Another instance running, exiting.", file=sys.stderr)
        sys.exit(0)


def run_query(conn):
    rows = conn.execute("""
        WITH bench_wr AS (
            SELECT e.benchmark_id, c.known_answer, wr.hypothesis_supported,
                   ce.is_cross_domain,
                   ROW_NUMBER() OVER (PARTITION BY e.benchmark_id ORDER BY wr.created_at DESC) AS rn
            FROM experiments e
            JOIN worker_results wr ON wr.experiment_id = e.id
            JOIN curiosities c ON c.benchmark_id = e.benchmark_id
            JOIN claim_evidence ce ON ce.experiment_id = e.id
            WHERE e.benchmark_id IS NOT NULL AND e.benchmark_id != ''
              AND c.known_answer IS NOT NULL AND c.known_answer IN ('true', 'false')
        )
        SELECT
            CASE WHEN is_cross_domain = 1 THEN 'cross_domain' ELSE 'in_domain' END AS grp,
            COUNT(*) AS n,
            SUM(CASE WHEN (known_answer = 'true' AND hypothesis_supported = 1)
                      OR (known_answer = 'false' AND hypothesis_supported = 0) THEN 1 ELSE 0 END) AS correct,
            ROUND(100.0 * SUM(CASE WHEN (known_answer = 'true' AND hypothesis_supported = 1)
                      OR (known_answer = 'false' AND hypothesis_supported = 0) THEN 1 ELSE 0 END) / COUNT(*), 1) AS acc
        FROM bench_wr WHERE rn = 1
        GROUP BY CASE WHEN is_cross_domain = 1 THEN 'cross_domain' ELSE 'in_domain' END
    """).fetchall()

    detail = conn.execute("""
        WITH bench_wr AS (
            SELECT e.benchmark_id, c.known_answer, wr.hypothesis_supported,
                   ce.is_cross_domain,
                   ROW_NUMBER() OVER (PARTITION BY e.benchmark_id ORDER BY wr.created_at DESC) AS rn
            FROM experiments e
            JOIN worker_results wr ON wr.experiment_id = e.id
            JOIN curiosities c ON c.benchmark_id = e.benchmark_id
            JOIN claim_evidence ce ON ce.experiment_id = e.id
            WHERE e.benchmark_id IS NOT NULL AND e.benchmark_id != ''
              AND c.known_answer IS NOT NULL AND c.known_answer IN ('true', 'false')
        )
        SELECT
            CASE WHEN is_cross_domain = 1 THEN 'cross' ELSE 'in_dom' END AS grp,
            known_answer,
            COUNT(*) AS n,
            ROUND(100.0 * SUM(CASE WHEN (known_answer = 'true' AND hypothesis_supported = 1)
                      OR (known_answer = 'false' AND hypothesis_supported = 0) THEN 1 ELSE 0 END) / COUNT(*), 1) AS acc
        FROM bench_wr WHERE rn = 1
        GROUP BY CASE WHEN is_cross_domain = 1 THEN 'cross' ELSE 'in_dom' END, known_answer
        ORDER BY grp, known_answer
    """).fetchall()

    return rows, detail


def main():
    quiet = "--quiet" in sys.argv
    lock_fd = acquire_lock()

    try:
        conn = get_db()
        rows, detail = run_query(conn)

        result = {}
        for r in rows:
            result[r["grp"]] = {"n": r["n"], "correct": r["correct"], "accuracy": r["acc"]}

        in_dom = result.get("in_domain", {})
        cross = result.get("cross_domain", {})
        gap = (in_dom.get("accuracy", 0) or 0) - (cross.get("accuracy", 0) or 0)

        detail_dict = {}
        for d in detail:
            key = f"{d['grp']}_{d['known_answer']}"
            detail_dict[key] = {"n": d["n"], "accuracy": d["acc"]}

        status = {
            "timestamp": time.time(),
            "in_domain": in_dom,
            "cross_domain": cross,
            "gap_pp": round(gap, 1),
            "threshold_pp": GAP_THRESHOLD,
            "exceeded": gap >= GAP_THRESHOLD,
            "detail": detail_dict,
            "baseline_20260626": {"in_domain": 83.7, "cross_domain": 78.8, "gap": 4.9},
        }

        with open(STATUS_PATH, "w") as f:
            json.dump(status, f, indent=2)

        if gap >= GAP_THRESHOLD:
            alert = (
                f"ALERT: BENCH3 cross-domain gap {gap:.1f}pp exceeds {GAP_THRESHOLD}pp threshold. "
                f"in_domain={in_dom.get('accuracy')}% (n={in_dom.get('n')}), "
                f"cross_domain={cross.get('accuracy')}% (n={cross.get('n')}). "
                f"False-detection: in_dom={detail_dict.get('in_dom_false', {}).get('accuracy')}%, "
                f"cross={detail_dict.get('cross_false', {}).get('accuracy')}%. "
                f"See {STATUS_PATH}"
            )
            if not quiet:
                print(alert)
            sys.exit(1)
        else:
            # Clean run — empty stdout (no_agent contract)
            sys.exit(0)

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
