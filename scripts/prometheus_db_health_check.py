#!/usr/bin/env python3
"""
prometheus_db_health_check.py — Verify prometheus.db integrity.

Runs every 2 minutes via cron. Checks:
1. experiments table exists
2. experiment count > 1000 (minimum viable)
3. No tables missing (should have 21)
4. File size > 1MB (not truncated)

Exit codes:
  0 = healthy
  1 = degraded (warning)
  2 = critical (corruption detected — alert immediately)

Output is consumed by the cron agent for alerting.
"""
import os
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import sqlite3
import sys

DB_PATH = _PP_PROMETHEUS_DB
EXPECTED_TABLES = 21
MIN_EXPERIMENTS = 1000
MIN_FILE_SIZE = 1_000_000  # 1MB


def check():
    issues = []
    critical = False

    # 1. File exists and is reasonable size
    if not os.path.exists(DB_PATH):
        print("CRITICAL: prometheus.db does not exist!")
        return 2

    file_size = os.path.getsize(DB_PATH)
    if file_size < MIN_FILE_SIZE:
        issues.append(f"File size {file_size:,} bytes (expected >{MIN_FILE_SIZE:,})")
        critical = True

    # 2. Can we open and read the DB?
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
    except Exception as e:
        print(f"CRITICAL: Cannot open database: {e}")
        return 2

    try:
        # 3. Check tables
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]

        if "experiments" not in tables:
            issues.append("experiments table MISSING")
            critical = True
        elif "worker_results" not in tables:
            issues.append("worker_results table MISSING")
            critical = True
        elif "synthesis_outputs" not in tables:
            issues.append("synthesis_outputs table MISSING")
            critical = True

        if len(tables) < EXPECTED_TABLES - 3:  # Allow for minor variations
            issues.append(f"Only {len(tables)} tables (expected ~{EXPECTED_TABLES})")

        # 4. Check experiment count
        if "experiments" in tables:
            count = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            if count < MIN_EXPERIMENTS:
                issues.append(f"Only {count} experiments (minimum: {MIN_EXPERIMENTS})")
                critical = True
            else:
                # Also check for corruption indicators
                # If count is very low but worker_results has many unapplied, something's wrong
                if "worker_results" in tables:
                    unapplied = conn.execute(
                        "SELECT COUNT(*) FROM worker_results WHERE applied=0"
                    ).fetchone()[0]
                    if unapplied > 100 and count < 1000:
                        issues.append(
                            f"Low experiment count ({count}) with {unapplied} unapplied worker results"
                        )
                        critical = True

    finally:
        conn.close()

    # Report
    if critical:
        print(f"CRITICAL: {'; '.join(issues)}")
        print(f"File: {DB_PATH} ({file_size:,} bytes)")
        print(f"Tables: {len(tables) if 'tables' in dir() else 'unknown'}")
        print("ACTION: Restore from backup immediately. DO NOT recreate tables.")
        return 2
    elif issues:
        print(f"DEGRADED: {'; '.join(issues)}")
        return 1
    else:
        # Healthy — silent output
        if "count" in dir():
            print(f"OK: {count} experiments, {len(tables)} tables, {file_size:,} bytes")
        return 0


if __name__ == "__main__":
    sys.exit(check())
