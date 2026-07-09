#!/usr/bin/env python3
"""
create_synthesis_tasks.py — Create synthesis tasks for the default worker.

FIXED (June 17 2026): The original script blindly created 12 SYNTHESIS tasks every
3 minutes without checking how many already existed in ready/running/archived state.
Workers archived them, but the script only checked status='ready', so it kept
recreating identical tasks in an infinite loop.

FIXED (June 21 2026): get_existing_titles() checked status != 'archived', which
included 'done' tasks. Since all 12 titles were in 'done' status, the dedup
blocked ALL creation. Changed to only check ready+running status — synthesis
tasks should be recreated per cycle since each run covers different experiments.

This version:
  1. Checks how many SYNTHESIS tasks already exist in ready+running state
  2. Only creates new ones if below MAX_READY threshold
  3. Deduplicates against active (ready/running) SYNTHESIS tasks only
  4. Caps total created per run at MAX_CREATE
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from db_retry import get_db

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
HERMES_HOME = os.path.expanduser("~/.hermes")
SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")

MAX_READY = 5          # Don't create if this many synthesis tasks already ready+running
MAX_CREATE = 5         # Max tasks to create per run (was 12, reduced to match worker capacity)

SYNTHESIS_TITLES = [
    "SYNTHESIS: Sparse domains focus",
    "SYNTHESIS: Cross-domain patterns",
    "SYNTHESIS: Transfer mechanisms",
    "SYNTHESIS: Under-represented fields",
    "SYNTHESIS: Novel connections",
    "SYNTHESIS: Contradiction probes",
    "SYNTHESIS: Boundary conditions",
    "SYNTHESIS: Edge cases and failures",
    "SYNTHESIS: Mechanism depth",
    "SYNTHESIS: Assumption challenges",
    "SYNTHESIS: Quantitative predictions",
    "SYNTHESIS: Negative results synthesis",
]


def get_synthesis_task_count():
    """Count existing synthesis tasks in ready or running state."""
    try:
        conn = get_db(db_path=KANBAN_DB)
        ready = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee='default' AND status='ready'"
        ).fetchone()[0]
        running = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE assignee='default' AND status='running'"
        ).fetchone()[0]
        conn.close()
        return ready, running
    except Exception as e:
        print(f"WARNING: Failed to count synthesis tasks: {e}")
        return 0, 0


def get_existing_titles():
    """Get all active (ready/running) synthesis task titles to avoid duplicates.
    Only checks ready+running — done/archived tasks can be recreated since each
    run covers different experiments."""
    try:
        conn = get_db(db_path=KANBAN_DB)
        rows = conn.execute(
            "SELECT lower(title) FROM tasks WHERE assignee='default' AND status IN ('ready', 'running')"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception as e:
        print(f"WARNING: Failed to get existing titles: {e}")
        return set()


def generate_body():
    """Generate synthesis task body via generate_synthesis_body.py."""
    try:
        result = subprocess.run(
            ["python3", os.path.join(SCRIPTS_DIR, "generate_synthesis_body.py")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HERMES_HOME": HERMES_HOME},
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        else:
            print(f"WARNING: generate_synthesis_body.py failed: {result.stderr[:200]}")
            # Fallback minimal body
            return (
                "SYNTHESIS TASK\n\n"
                "Query the Prometheus database for recent completed experiments.\n"
                "Find cross-domain patterns, generate WHY IT WORKS mechanisms,\n"
                "and identify [TRANSFER] curiosities for the curiosity queue.\n\n"
                "Use get_synthesis_context.py to get experiment data.\n"
                "Write results to synthesis_outputs table.\n"
            )
    except Exception as e:
        print(f"WARNING: generate_synthesis_body.py error: {e}")
        return "SYNTHESIS TASK — query database for recent experiments and find patterns.\n"


def main():
    ready, running = get_synthesis_task_count()
    print(f"Synthesis tasks: {ready} ready, {running} running")

    # Don't create if we already have enough in the pipeline
    if ready + running >= MAX_READY:
        print(f"SKIP: {ready + running} synthesis tasks already in pipeline (max={MAX_READY})")
        return

    # How many to create
    to_create = min(MAX_READY - ready - running, MAX_CREATE)
    if to_create <= 0:
        print("SKIP: No slots available")
        return

    # Get existing titles to avoid duplicates
    existing = get_existing_titles()

    # Generate body
    body = generate_body()
    if not body:
        print("ERROR: Failed to generate body, aborting")
        return

    # Create tasks
    conn = get_db(db_path=KANBAN_DB)

    created = 0
    now = int(time.time())

    for title in SYNTHESIS_TITLES:
        if created >= to_create:
            break
        if title.lower() in existing:
            continue  # Skip duplicate

        tid = "t_" + uuid.uuid4().hex[:8]
        try:
            conn.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, priority, "
                "created_by, created_at, workspace_kind) "
                "VALUES (?, ?, ?, ?, 'ready', 2, 'director', ?, 'scratch')",
                (tid, title, body, "default", now),
            )
            created += 1
            existing.add(title.lower())
            print(f"  Created: {title} ({tid})")
        except Exception as e:
            print(f"  FAILED: {title}: {e}")

    conn.commit()
    conn.close()
    print(f"Created {created} synthesis tasks (target was {to_create})")


if __name__ == "__main__":
    main()
