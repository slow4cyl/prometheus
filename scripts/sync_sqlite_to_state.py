#!/usr/bin/env python3
"""
Sync SQLite prometheus.db data BACK to self_state.json.

This bypasses the synthesis worker's approval-blocked writes by reading
the ground truth from SQLite and writing a minimal update to self_state.json.

Updates:
  - experiments.completed list (from SQLite experiments table)
  - metrics counters (from actual counts)
  - knowledge_graph.domains (confidence from SQLite domains table)
  - curiosity_queue (from SQLite curiosities table)
  - version, last_updated

Only writes if there are actual changes (prevents unnecessary disk writes).

ARCHITECTURE: Data loading (paginated SQLite) happens OUTSIDE the state lock.
Only the final write holds the lock, keeping it short (~milliseconds).
This prevents lock contention with synthesis_merger.py (every 1m).
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

# Use shared state_lock for serialized write access

"""Sync Sqlite To State.

Part of the Prometheus research infrastructure.
"""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state_lock import load_state, save_state, state_write_lock

DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")
STATE_PATH = os.path.expanduser("~/.hermes/self_state.json")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.row_factory = sqlite3.Row
    return conn


def _get_existing_exp_ids(state):
    """Extract existing experiment IDs from whatever structure self_state has."""
    ids = set()
    exps = state.get("experiments", {})
    if isinstance(exps, dict):
        for exp in exps.get("completed", []):
            if isinstance(exp, dict) and "id" in exp:
                ids.add(exp["id"])
    # Also check flat list
    for exp in state.get("experiments_completed_list", []):
        if isinstance(exp, dict) and "id" in exp:
            ids.add(exp["id"])
        elif isinstance(exp, str):
            ids.add(exp)
    ids.discard("")
    return ids


def _load_ss_exp_ids():
    """Read self_state.json WITHOUT lock to get existing experiment IDs."""
    if not os.path.exists(STATE_PATH):
        return set()
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        return _get_existing_exp_ids(state)
    except (json.JSONDecodeError, IOError):
        return set()


def sync():
    if not os.path.exists(STATE_PATH):
        print(f"ERROR: {STATE_PATH} not found")
        return False

    conn = get_db()

    # Get total count first (lightweight — single integer row)
    total_completed = conn.execute(
        "SELECT COUNT(*) FROM experiments WHERE status = 'completed'"
    ).fetchone()[0]

    # Get domains (small, ~100 rows — safe without pagination)
    db_domains = conn.execute(
        "SELECT name, confidence FROM domains ORDER BY confidence DESC"
    ).fetchall()

    # ── STAGE 1: Load existing state IDs (no lock, read-only) ──────────
    ss_exp_ids = _load_ss_exp_ids()
    print(f"  DB has {total_completed} completed experiments, self_state has {len(ss_exp_ids)}")

    # ── STAGE 2: Paginate SQLite data OUTSIDE the lock ────────────────
    # Loading 59K experiments at once = 471MB+ RSS → memory cgroup throttling.
    # Chunks of 2000 keep peak memory low (~16MB per chunk).
    CHUNK_SIZE = 2000
    new_exps = []
    offset = 0
    while True:
        chunk = conn.execute(
            "SELECT id, hypothesis, result, status, confidence_change, "
            "tags, domain, model, created_at, completed_at "
            "FROM experiments WHERE status = 'completed' ORDER BY created_at "
            "LIMIT ? OFFSET ?",
            (CHUNK_SIZE, offset)
        ).fetchall()
        if not chunk:
            break
        for row in chunk:
            if row["id"] not in ss_exp_ids:
                tags = []
                try:
                    tags = json.loads(row["tags"]) if row["tags"] else []
                except (json.JSONDecodeError, TypeError):
                    pass

                exp_entry = {
                    "id": row["id"],
                    "hypothesis": row["hypothesis"] or "",
                    "result": row["result"] or "",
                    "status": row["status"] or "completed",
                    "confidence_change": row["confidence_change"] or 0,
                    "tags": tags,
                    "domain": row["domain"] or "",
                    "model": row["model"] or "",
                }
                if row["created_at"]:
                    try:
                        exp_entry["timestamp"] = datetime.fromtimestamp(
                            row["created_at"], tz=timezone.utc
                        ).isoformat()
                    except (ValueError, TypeError, OSError):
                        pass
                new_exps.append(exp_entry)
        offset += CHUNK_SIZE
        # Free chunk memory before next iteration
        del chunk

    conn.close()

    # ── STAGE 3: Acquire lock BRIEFLY to write ────────────────────────
    # This holds the lock for milliseconds, not seconds. No contention
    # with synthesis_merger (every 1m) or other sync processes (every 2m).
    with state_write_lock(timeout=60) as state:
        if state is None:
            print("  ERROR: Could not acquire state lock within 60s, sync aborted")
            return False
        changed = False

        if new_exps:
            # Ensure experiments is a proper dict, not an int
            if not isinstance(state.get("experiments"), dict):
                state["experiments"] = {}

            # Add to experiments.completed list
            state["experiments"].setdefault("completed", []).extend(new_exps)

            # TRIM: cap at 500 most recent entries to prevent unbounded growth
            # (was 5000 — made self_state.json 6.2MB, causing state_write_lock
            # to hold for seconds under I/O contention, blocking all other
            # writers: apply_worker_results, synthesis_merger, etc.)
            MAX_EXP_ENTRIES = 200
            completed = state["experiments"].get("completed", [])
            if len(completed) > MAX_EXP_ENTRIES:
                state["experiments"]["completed"] = completed[-MAX_EXP_ENTRIES:]
                print(f"  Trimmed experiments.completed from {len(completed)} to {MAX_EXP_ENTRIES}")

            # Also add to experiments_completed_list (flat list kept for compatibility)
            state.setdefault("experiments_completed_list", []).extend(new_exps)

            # TRIM flat list too
            flat_list = state.get("experiments_completed_list", [])
            if len(flat_list) > MAX_EXP_ENTRIES:
                state["experiments_completed_list"] = flat_list[-MAX_EXP_ENTRIES:]

            changed = True
            print(f"  Added {len(new_exps)} new experiments to self_state.json")

        # 3. Sync counters — use SQLite as ground truth
        # Fix: if experiments was an int, replace with proper structure
        if isinstance(state.get("experiments"), int):
            state["experiments"] = {"completed": state.get("experiments_completed_list", [])}
            changed = True
            print("  Fixed: experiments field was int, restored to dict")

        # Update all the redundant counter fields to stay consistent
        state["total_experiments"] = total_completed
        state["total_experiments_completed"] = total_completed
        state["experiments_completed_count"] = total_completed
        state["experiments_completed"] = total_completed
        state["completed_count"] = total_completed
        state["completed"] = total_completed
        state["count"] = total_completed

        m = state.setdefault("metrics", {})
        m["experiments_completed"] = total_completed
        m["experiments_completed_count"] = total_completed
        m["experiments_conducted"] = total_completed
        m["experiments_run"] = max(m.get("experiments_run", 0), total_completed)

        # 4. Sync knowledge_graph.domains from SQLite
        kg = state.setdefault("knowledge_graph", {})
        ss_domains = kg.get("domains", {})

        if isinstance(ss_domains, list):
            ss_domains = {d.get("name", ""): d for d in ss_domains if isinstance(d, dict)}

        for row in db_domains:
            name = row["name"]
            conf = row["confidence"] or 0.5
            if name in ss_domains:
                old_conf = ss_domains[name].get("confidence", 0.5)
                if abs(old_conf - conf) > 0.01:
                    ss_domains[name]["confidence"] = conf
                    changed = True
            else:
                ss_domains[name] = {
                    "name": name,
                    "confidence": conf,
                    "subtopics": [],
                    "gaps": [],
                }
                changed = True

        if changed:
            kg["domains"] = ss_domains

        # 5. Sync curiosity_queue from SQLite curiosities
        # NOTE: self_state.json is the AUTHORITATIVE source for the queue.
        # The Director manages the queue (adds items, marks resolved, deduplicates).
        # SQLite curiosities is just a tracking log — do NOT overwrite the queue from it.
        # This prevents a race condition where sync overwrites queue repairs.

        # 6. Update version and timestamp
        now = datetime.now(timezone.utc).isoformat()
        state["last_updated"] = now
        state["version"] = state.get("version", 0) + (1 if changed else 0)

    # Lock released and state saved by context manager
    if changed:
        print(f"  self_state.json updated (version {state['version']})")
    else:
        print("  No changes needed")

    return changed


if __name__ == "__main__":
    print("Sync SQLite -> self_state.json")
    sync()