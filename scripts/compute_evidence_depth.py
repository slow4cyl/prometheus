#!/usr/bin/env python3
"""
compute_evidence_depth.py — Compute evidence_depth (and generation_depth) for curiosities.

Fast version: uses parent-lookup instead of recursive CTE.

Two depths per curiosity (2026-07-02 — the depth audit found the depth-250+
front was 99% question-generation with no experiments behind it; see
architecture-changelog):

  generation_depth  raw lineage hops: parent's + 1, unconditionally. This is
                    the pre-audit evidence_depth semantics, kept for
                    lineage bookkeeping.
  evidence_depth    EXPERIMENT-BACKED depth: parent's + 1 only when the step
                    is backed by a real run — the child was minted from a
                    worker result (source_result_id) OR its parent question
                    was actually answered (resolved_by_experiment). Synthetic
                    hops (synthesis, injection, question mutation) inherit the
                    parent's evidence_depth unchanged: they carry depth, they
                    do not earn it.

Everything that consumes evidence_depth (scoring bonus, deep-lineage queue
tier, DEEP_FLOOR retirement exemption, health-snapshot DEPTH panel) therefore
keys on verified progress, not generator recursion.

Usage:
    python3 compute_evidence_depth.py          # compute for all
    python3 compute_evidence_depth.py --recent  # only curiosities from last 2 hours
"""

import sqlite3
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import os
import time
import argparse
from db_retry import get_db

DB = _PP_PROMETHEUS_DB
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent", action="store_true", help="Only compute for recent curiosities")
    args = parser.parse_args()

    conn = get_db()
    conn.row_factory = sqlite3.Row

    # Ensure columns exist
    for ddl in ("ALTER TABLE curiosities ADD COLUMN evidence_depth INTEGER DEFAULT 0",
                "ALTER TABLE curiosities ADD COLUMN generation_depth INTEGER DEFAULT 0"):
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Select questions to update
    if args.recent:
        cutoff = time.time() - 7200
        rows = conn.execute("""
            SELECT id, parent_curiosity_id, source_result_id,
                   COALESCE(evidence_depth, 0) as current_depth,
                   COALESCE(generation_depth, 0) as current_gen
            FROM curiosities
            WHERE created_at > ? AND parent_curiosity_id IS NOT NULL
        """, (cutoff,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT id, parent_curiosity_id, source_result_id,
                   COALESCE(evidence_depth, 0) as current_depth,
                   COALESCE(generation_depth, 0) as current_gen
            FROM curiosities
            WHERE parent_curiosity_id IS NOT NULL
        """).fetchall()

    print(f"Computing depth for {len(rows)} questions with parents")

    updated = 0
    errors = 0
    for r in rows:
        parent = conn.execute(
            "SELECT COALESCE(evidence_depth, 0) as depth, "
            "       COALESCE(generation_depth, 0) as gen, "
            "       resolved_by_experiment "
            "FROM curiosities WHERE id = ?",
            (r['parent_curiosity_id'],)
        ).fetchone()

        if parent:
            # Experiment-backed step: born from a worker result, or the parent
            # question was actually answered. Only these earn evidence depth.
            backed = (r['source_result_id'] is not None
                      or parent['resolved_by_experiment'] is not None)
            new_depth = parent['depth'] + (1 if backed else 0)
            new_gen = parent['gen'] + 1

            if new_depth != r['current_depth'] or new_gen != r['current_gen']:
                try:
                    conn.execute(
                        "UPDATE curiosities SET evidence_depth = ?, generation_depth = ? WHERE id = ?",
                        (new_depth, new_gen, r['id']))
                    updated += 1
                except sqlite3.OperationalError:
                    errors += 1
                    # Retry once
                    time.sleep(1)
                    try:
                        conn.execute(
                            "UPDATE curiosities SET evidence_depth = ?, generation_depth = ? WHERE id = ?",
                            (new_depth, new_gen, r['id']))
                        updated += 1
                        errors -= 1
                    except:
                        pass

    try:
        conn.commit()
    except:
        time.sleep(2)
        conn.commit()

    print(f"Updated {updated} questions, {errors} errors")

    # Stats
    print("\n=== EVIDENCE DEPTH DISTRIBUTION (active) ===")
    for r in conn.execute("""
        SELECT COALESCE(evidence_depth, 0) as depth, COUNT(*) as n
        FROM curiosities WHERE status='active'
        GROUP BY depth ORDER BY depth LIMIT 15
    """):
        print(f"  depth {r[0]}: {r[1]}")

    conn.close()

if __name__ == "__main__":
    main()
