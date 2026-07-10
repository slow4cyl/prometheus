#!/usr/bin/env python3
"""
result_bridge.py — Ensures task completion results reach worker_results.

Problem: Workers complete tasks via kanban_complete (writes to task_runs.summary 
and task_runs.metadata in kanban.db) but don't always call write_worker_result.py 
(which writes to worker_results in prometheus.db). This creates a gap where 
results exist in kanban but are invisible to apply_worker_results.py, which only 
reads from worker_results.

Solution: This script runs periodically and:
1. Finds done tasks in kanban.db that have task_runs summaries but NO worker_results entry
2. Extracts the verdict (SUPPORTED/REFUTED) from task_runs metadata or summary text
3. Inserts a worker_results row so apply_worker_results.py can process it

This is a safety net, not the primary path. Workers should still call 
write_worker_result.py. This catches the cases where they don't.
"""

import json
import os
import re
import sys
import time
import fcntl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_retry

LOCK_FILE = os.path.expanduser("~/.hermes/result_bridge.lock")


def acquire_lock():
    """Single-instance lock — prevent concurrent runs."""
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def release_lock(fd):
    if fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


def get_done_tasks_without_results(kconn, pconn):
    """Find done tasks with task_runs summaries but no worker_results entry."""
    done_tasks = kconn.execute("""
        SELECT DISTINCT t.id, t.title, t.assignee
        FROM tasks t
        JOIN task_runs tr ON t.id = tr.task_id
        WHERE t.status = 'done'
          AND tr.summary IS NOT NULL
          AND tr.summary != ''
    """).fetchall()
    
    missing = []
    for tid, title, assignee in done_tasks:
        exists = pconn.execute(
            "SELECT 1 FROM worker_results WHERE kanban_task_id = ? LIMIT 1",
            (tid,)
        ).fetchone()
        if not exists:
            missing.append((tid, title, assignee))
    
    return missing


def cap_confidence(val):
    """Cap individual worker result confidence at 0.85.
    
    Higher confidence requires system-level aggregation across
    multiple independent replications.
    """
    if val is None:
        return 0.5
    try:
        v = float(val)
    except (ValueError, TypeError):
        return 0.5
    if v > 0.85:
        return 0.85
    if v < 0:
        return 0.0
    return v


def extract_verdict(summary, metadata_json):
    """Extract SUPPORTED/REFUTED verdict from metadata or summary text."""
    if metadata_json:
        try:
            meta = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
            verdict = str(meta.get("verdict", "")).upper()
            if "SUPPORT" in verdict or "CONFIRM" in verdict:
                return 1, cap_confidence(meta.get("confidence", 0.5))
            if "REFUT" in verdict:
                return 0, cap_confidence(meta.get("confidence", 0.5))
        except (json.JSONDecodeError, AttributeError):
            pass
    
    if not summary:
        return None, None
    
    s = summary.lower()
    prefix = s[:200]
    
    refuted_signals = ["refuted", "hypothesis refuted", "not supported",
                       "disproven", "falsified", "claim is false", "incorrect"]
    supported_signals = ["confirmed", "supported", "hypothesis supported",
                         "verified", "validated"]
    
    for sig in refuted_signals:
        if sig in prefix:
            return 0, 0.7
    
    for sig in supported_signals:
        if sig in prefix:
            return 1, 0.7
    
    return None, None


def bridge_results(kconn, pconn):
    """Main: find missing results and bridge them."""
    missing = get_done_tasks_without_results(kconn, pconn)
    
    if not missing:
        print("result_bridge: all done tasks have worker_results — nothing to bridge")
        return 0
    
    bridged = 0
    skipped = 0
    
    for tid, title, assignee in missing:
        run = kconn.execute(
            "SELECT summary, metadata, started_at, ended_at FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (tid,)
        ).fetchone()
        
        if not run:
            skipped += 1
            continue
        
        summary, metadata_json, started_at, ended_at = run
        supported, confidence = extract_verdict(summary, metadata_json)
        
        if supported is None:
            skipped += 1
            continue
        
        exp_id = None
        title_match = re.search(r'exp_(\w+)', title or '')
        if title_match:
            exp_id = f"exp_{title_match.group(1)}"
        else:
            body_row = kconn.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()
            if body_row:
                body_match = re.search(r'exp_(\w+)', body_row[0] or '')
                if body_match:
                    exp_id = f"exp_{body_match.group(1)}"
        
        if not exp_id:
            exp_id = f"exp_bridge_{tid}"
        
        now = int(time.time())
        domain = ""  # was "unknown" — empty so apply_worker_results classifies it

        if metadata_json:
            try:
                meta = json.loads(metadata_json) if isinstance(metadata_json, str) else metadata_json
                if "domain" in meta:
                    domain = meta["domain"]
            except (json.JSONDecodeError, TypeError):
                pass
        
        try:
            pconn.execute("""
                INSERT OR IGNORE INTO worker_results 
                (experiment_id, kanban_task_id, hypothesis_supported, key_finding, 
                 confidence, domain, created_at, applied, supported, finding)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """, (exp_id, tid, supported, summary or "", cap_confidence(confidence), domain, now, supported, summary or ""))
            bridged += 1
        except Exception as e:
            print(f"  ERROR bridging {tid}: {e}")
            skipped += 1
    
    pconn.commit()
    print(f"result_bridge: bridged {bridged}, skipped {skipped} (ambiguous/no runs), out of {len(missing)} missing")
    return bridged


def main():
    # Single-instance lock
    lock_fd = acquire_lock()
    if lock_fd is None:
        print("result_bridge: another instance running, skipping")
        return
    
    try:
        kconn = db_retry.get_db(db_retry.KANBAN_DB)
        pconn = db_retry.get_db(db_retry.PROMETHEUS_DB)
        
        bridge_results(kconn, pconn)
    except Exception as e:
        print(f"result_bridge ERROR: {e}")
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
