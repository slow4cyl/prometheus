#!/usr/bin/env python3
"""
Director health check — single-script bulk check of all running Kanban tasks.

Usage:
    python3 ~/.hermes/skills/devops/kanban-worker/scripts/director_health_check.py

Reads /tmp/kanban_running.json (dump via: hermes kanban list --status running --json > /tmp/kanban_running.json)
and checks each task's workspace for liveness. Outputs a table with status per task.

Statuses:
    ACTIVE  (<5m since last file write) — worker is progressing
    SLOW    (5-15m) — likely in an API call, verify with ps aux
    STALE   (>15m) — may be stuck, cross-reference with ps aux
    NO_WS   — no workspace directory found
    EMPTY   — workspace exists but has no files

Combine with: ps aux | grep hermes | grep kanban  (ground truth for process liveness)
"""
import json, os, sys, time

WORKSPACES_BASE = os.path.expanduser('~/.hermes/kanban/workspaces')
RUNNING_FILE = '/tmp/kanban_running.json'

def check_task(task):
    tid = task['id']
    title = task.get('title', 'N/A')[:50]
    ws = os.path.join(WORKSPACES_BASE, tid)
    
    if not os.path.isdir(ws):
        return tid, title, 'NO_WS', 999, 0
    
    files = [f for f in os.listdir(ws) if os.path.isfile(os.path.join(ws, f))]
    if not files:
        return tid, title, 'EMPTY', 999, 0
    
    latest_mtime = max(os.path.getmtime(os.path.join(ws, f)) for f in files)
    age_min = (time.time() - latest_mtime) / 60
    
    if age_min < 5:
        status = 'ACTIVE'
    elif age_min < 15:
        status = 'SLOW'
    else:
        status = 'STALE'
    
    return tid, title, status, age_min, len(files)

def main():
    if not os.path.exists(RUNNING_FILE):
        print(f"ERROR: {RUNNING_FILE} not found. Run first:")
        print(f"  hermes kanban list --status running --json 2>&1 > {RUNNING_FILE}")
        sys.exit(1)
    
    tasks = json.load(open(RUNNING_FILE))
    print(f"{'Task ID':<14} {'Status':<7} {'Age':>5} {'Files':>5}  Title")
    print('-' * 90)
    
    counts = {'ACTIVE': 0, 'SLOW': 0, 'STALE': 0, 'NO_WS': 0, 'EMPTY': 0}
    for task in tasks:
        tid, title, status, age_min, n_files = check_task(task)
        counts[status] += 1
        print(f"{tid:<14} {status:<7} {age_min:>4.0f}m {n_files:>5}  {title}")
    
    print('-' * 90)
    print(f"Total: {len(tasks)} | " + ' | '.join(f"{k}:{v}" for k, v in counts.items() if v))

if __name__ == '__main__':
    main()
