#!/usr/bin/env python3
"""Recovery sweep: retroactively write worker_results for experiment tasks
that completed in kanban without calling write_worker_result.py.

Targets done tasks with exp_* titles that have a kanban result string but
no corresponding worker_results row.  Parses the kanban result to extract
an experiment ID and finding text, then inserts into worker_results.

Usage:
    python3 recovery_sweep.py [--dry-run]

After running, apply results:
    python3 ~/.hermes/scripts/apply_worker_results.py
"""

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
PROMETHEUS_DB = os.path.expanduser("~/.hermes/prometheus.db")


def extract_exp_id(title: str):
    """Extract experiment ID from task title (e.g. 'exp_8501: ...' -> 'exp_8501')."""
    m = re.match(r"(exp_\d+)", title.strip(), re.IGNORECASE)
    return m.group(1) if m else None


def main():
    parser = argparse.ArgumentParser(description="Recovery sweep for lost worker_results")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    kdb = sqlite3.connect(KANBAN_DB)
    pdb = sqlite3.connect(PROMETHEUS_DB)

    # Get all worker_results experiment_ids to avoid duplicates
    existing = set(
        r[0]
        for r in pdb.execute("SELECT experiment_id FROM worker_results").fetchall()
    )

    # Get done experiment tasks with kanban results but no worker_results
    rows = kdb.execute(
        """
        SELECT id, title, result, completed_at
        FROM tasks
        WHERE status = 'done'
          AND title LIKE 'exp_%'
          AND result IS NOT NULL
          AND result != ''
        ORDER BY completed_at DESC
    """
    ).fetchall()

    recovered = 0
    skipped = 0

    for task_id, title, result, completed_at in rows:
        exp_id = extract_exp_id(title)
        if not exp_id:
            skipped += 1
            continue

        if exp_id in existing:
            skipped += 1
            continue

        # Parse the kanban result as the finding
        finding = result.strip()[:2000]

        # Determine confidence from result text
        confidence = 0.7  # default
        result_upper = finding.upper()
        if "CONFIRMED" in result_upper or "SUPPORTED" in result_upper:
            confidence = 0.8
        elif "REFUTED" in result_upper or "REJECTED" in result_upper:
            confidence = 0.8
        elif "PARTIAL" in result_upper:
            confidence = 0.6

        # Determine domain from title
        domain = "security"  # default

        # Determine tags from result
        tags = []
        if "CONFIRMED" in result_upper:
            tags.append("CONFIRMED")
        if "REFUTED" in result_upper:
            tags.append("REFUTED")
        if "SUPPORTED" in result_upper:
            tags.append("SUPPORTED")
        if "BREAKTHROUGH" in result_upper:
            tags.append("BREAKTHROUGH")
        if "DISCOVERY" in result_upper or "DISCOVER" in result_upper:
            tags.append("DISCOVERY")
        tags_str = ",".join(tags) if tags else "RECOVERED"

        if args.dry_run:
            print(f"  [DRY] {exp_id} from {task_id}: {finding[:80]}...")
            recovered += 1
            continue

        # Insert into worker_results
        pdb.execute(
            """
            INSERT INTO worker_results (
                experiment_id, kanban_task_id, hypothesis_supported,
                key_finding, confidence, domain, tags,
                files_produced, queue_additions, worker_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                exp_id,
                task_id,
                1 if "CONFIRMED" in result_upper or "SUPPORTED" in result_upper else 0,
                finding,
                confidence,
                domain,
                tags_str,
                "",
                "",
                "recovery-sweep",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        existing.add(exp_id)
        recovered += 1

    pdb.commit()
    kdb.close()
    pdb.close()

    print(f"\nRecovery sweep complete:")
    print(f"  Recovered: {recovered}")
    print(f"  Skipped (already had results or no exp_id): {skipped}")
    if not args.dry_run and recovered > 0:
        print(f"\n  Run apply_worker_results.py to sync to experiments table:")
        print(f"    python3 ~/.hermes/scripts/apply_worker_results.py")


if __name__ == "__main__":
    main()
