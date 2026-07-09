#!/usr/bin/env python3
"""
queue_composition_monitor.py — Monitor kanban queue for synthesis dominance.

Reads kanban.db, calculates the ratio of SYNTHESIS tasks to total tasks
in ready+running state at each run. If ratio exceeds 80% consistently,
creates a kanban task for the Director's next cycle to investigate.

Uses db_retry for database access to handle contention with 50 workers.

Usage:
    python3 queue_composition_monitor.py          # check and report
    python3 queue_composition_monitor.py --force  # force escalate regardless

Cron: every 2 minutes, no_agent=true. Output: status JSON + optional kanban task.
"""

import json
import os
import sys
import time
import uuid

# Paths
HERMES_HOME = os.path.expanduser("~/.hermes")
KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")
SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")
STATUS_PATH = os.path.join(HERMES_HOME, "queue_composition_status.json")
STATE_PATH = os.path.join(HERMES_HOME, "watchdog_state.json")

sys.path.insert(0, SCRIPTS_DIR)
from db_retry import get_db  # noqa: E402

# ── Thresholds ──────────────────────────────────────────────────────
SYNTHESIS_RATIO_THRESHOLD = 0.80   # 80% synthesis = queue imbalance
MIN_TASKS_FOR_ALERT = 10           # don't alert on nearly-empty queue
CONSECUTIVE_BEFORE_ESCALATION = 3  # create kanban task after 3 consecutive detections


def _load_state():
    """Load persistent state from watchdog_state.json."""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            return {}
    return {}


def _save_state(state):
    """Write persistent state atomically."""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _create_escalation_task(ratio, synth, total):
    """Create a kanban task for the Director to investigate synthesis dominance.

    Dedup: only creates if no existing ready/running task with the same prefix.
    """
    pct = round(ratio * 100, 1)
    title = f"[SELF-HEAL] Queue composition anomaly — {pct}% SYNTHESIS tasks ({synth}/{total})"
    body = (
        f"The queue composition monitor detected that {pct}% of ready+running tasks "
        f"are SYNTHESIS tasks ({synth} out of {total}).\n\n"
        f"This may indicate that experiment task creation (task_refiller) is stalled "
        f"or synthesis is overwhelming the queue.\n\n"
        f"Diagnostic steps:\n"
        f"1. Check if task_refiller.py is running: journalctl --user -u watchdog-of-watchdogs --since '5 min ago'\n"
        f"2. Check refiller_summary.json mtime: stat ~/.hermes/refiller_summary.json\n"
        f"3. Check for stuck scripts: grep 'STUCK SCRIPT' in watchdog journal\n"
        f"4. Check curiosity queue depth in self_state.json"
    )
    tid = "t_" + uuid.uuid4().hex[:8]
    now = int(time.time())

    conn = get_db(KANBAN_DB)

    # Dedup: don't create if a similar task is already pending
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM tasks "
            "WHERE title LIKE ? AND status IN ('ready', 'running')",
            ("[SELF-HEAL] Queue composition%",)
        ).fetchone()[0]
    except Exception:
        existing = 0

    if existing > 0:
        conn.close()
        print(f"  Escalation skipped — {existing} similar task(s) already pending")
        return

    try:
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, "
            "created_by, created_at, workspace_kind) "
            "VALUES (?, ?, ?, NULL, 'ready', 1, 'watchdog', ?, 'scratch')",
            (tid, title, body, now)
        )
        conn.commit()
        print(f"  Created kanban task {tid}: {title}")
    except Exception as e:
        print(f"  Failed to create escalation task: {e}")
    finally:
        conn.close()


def main():
    force = "--force" in sys.argv

    # ── Read kanban state ────────────────────────────────────────
    try:
        conn = get_db(KANBAN_DB)
        total = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('ready', 'running')"
        ).fetchone()[0]
        synth = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('ready', 'running') "
            "AND title LIKE 'SYNTHESIS:%'"
        ).fetchone()[0]
        conn.close()
    except Exception as e:
        print(f"ERROR: Failed to query kanban.db: {e}")
        # Write a status indicating failure
        status = {
            "timestamp": time.time(),
            "total_tasks": -1,
            "synthesis_tasks": -1,
            "ratio": -1,
            "alert": False,
            "consecutive": 0,
            "escalated": False,
            "error": str(e),
        }
        with open(STATUS_PATH, "w") as f:
            json.dump(status, f, indent=2)
        sys.exit(1)

    ratio = synth / total if total > 0 else 0.0

    # ── Load persistent state ────────────────────────────────────
    state = _load_state()
    sd = state.get("synthesis_dominance", {})
    consecutive = sd.get("consecutive", 0)
    already_escalated = sd.get("escalated", False)

    # ── Evaluate ─────────────────────────────────────────────────
    alert = (ratio > SYNTHESIS_RATIO_THRESHOLD and total >= MIN_TASKS_FOR_ALERT) or force

    if alert:
        consecutive += 1
        # Escalate if persistent and not already escalated
        should_escalate = consecutive >= CONSECUTIVE_BEFORE_ESCALATION and not already_escalated
        if should_escalate or force:
            _create_escalation_task(ratio, synth, total)
            already_escalated = True
    else:
        consecutive = 0
        already_escalated = False

    # ── Persist state ────────────────────────────────────────────
    state["synthesis_dominance"] = {
        "consecutive": consecutive,
        "escalated": already_escalated,
        "last_check": time.time(),
    }
    _save_state(state)

    # ── Write status file (read by watchdog) ─────────────────────
    pct = round(ratio * 100, 1)
    status = {
        "timestamp": time.time(),
        "total_tasks": total,
        "synthesis_tasks": synth,
        "ratio": round(ratio, 4),
        "pct": pct,
        "alert": alert,
        "consecutive": consecutive,
        "escalated": already_escalated,
    }
    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)

    # ── Output ───────────────────────────────────────────────────
    if alert:
        print(
            f"ALERT: {pct}% synthesis ({synth}/{total} tasks) — "
            f"threshold {SYNTHESIS_RATIO_THRESHOLD*100:.0f}%"
        )
        if already_escalated:
            print(f"  Escalated to kanban (consecutive={consecutive})")
        else:
            print(f"  Watching (consecutive={consecutive}/{CONSECUTIVE_BEFORE_ESCALATION})")
    else:
        print(f"OK: {pct}% synthesis ({synth}/{total} tasks)")


if __name__ == "__main__":
    main()
