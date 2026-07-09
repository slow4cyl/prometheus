#!/usr/bin/env python3
"""
Director combined health check — process liveness + workspace status in one pass.

Usage:
    hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json
    python3 ~/.hermes/skills/devops/kanban-worker/scripts/director_combined_health_check.py

Checks two signals per task:
  1. Process alive in ps aux (Method 1 — ground truth for liveness)
  2. Workspace file count + modification time (Methods 3+4 — output progress)

Statuses:
  HEALTHY    — process alive + workspace has recent output
  API_CALL   — process alive + workspace stale (likely in long API call, normal)
  HUNG       — process alive + workspace empty/stale for >60m (stuck, not progressing)
  DEAD       — process not found (crashed)
  NO_WS      — no workspace directory
  DISPATCHED — task just spawned, no files yet (normal, recheck in 5m)

Combine with: kanban_show <id> --json for heartbeat detail when HUNG or DEAD.
"""
import json, os, sys, subprocess, time

WORKSPACES_BASE = os.path.expanduser('~/.hermes/kanban/workspaces')
RUNNING_FILE = '/tmp/kanban_running.json'


def get_alive_pids():
    """Return set of task IDs found in ps aux output."""
    try:
        result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        return set(result.stdout)
    except Exception:
        return set()


def check_task(task, ps_output):
    tid = task['id']
    title = task.get('title', 'N/A')[:55]

    # Method 1: process liveness
    pid_alive = tid in ps_output

    # Methods 3+4: workspace status
    ws = os.path.join(WORKSPACES_BASE, tid)
    if not os.path.isdir(ws):
        if not pid_alive:
            return tid, title, 'DEAD', 999, 0, 'no process, no workspace'
        return tid, title, 'NO_WS', 999, 0, 'process alive but no workspace'

    files = [f for f in os.listdir(ws) if os.path.isfile(os.path.join(ws, f))]
    if not files:
        if not pid_alive:
            return tid, title, 'DEAD', 999, 0, 'no process, empty workspace'
        return tid, title, 'DISPATCHED', 999, 0, 'process alive, empty workspace (just spawned?)'

    latest_mtime = max(os.path.getmtime(os.path.join(ws, f)) for f in files)
    age_min = (time.time() - latest_mtime) / 60

    if not pid_alive:
        return tid, title, 'DEAD', age_min, len(files), f'no process, {len(files)} files, {age_min:.0f}m stale'

    # Process alive — classify workspace staleness
    if age_min < 5:
        status = 'HEALTHY'
        note = f'{len(files)} files, active'
    elif age_min < 15:
        status = 'API_CALL'
        note = f'{len(files)} files, likely API call'
    elif age_min < 60:
        status = 'API_CALL'
        note = f'{len(files)} files, long API call or computation'
    else:
        status = 'HUNG'
        note = f'{len(files)} files, {age_min:.0f}m stale — process alive but silent'

    return tid, title, status, age_min, len(files), note


def main():
    if not os.path.exists(RUNNING_FILE):
        print(f"ERROR: {RUNNING_FILE} not found. Run first:")
        print(f"  hermes kanban list --status running --json 2>&1 > {RUNNING_FILE}")
        sys.exit(1)

    tasks = json.load(open(RUNNING_FILE))
    ps_output = get_alive_pids()

    print(f"{'Task ID':<14} {'Status':<11} {'Age':>5} {'Files':>5}  Note")
    print('-' * 100)

    counts = {}
    hung_tasks = []
    dead_tasks = []

    for task in tasks:
        tid, title, status, age_min, n_files, note = check_task(task, ps_output)
        counts[status] = counts.get(status, 0) + 1
        marker = ' <<<' if status in ('HUNG', 'DEAD') else ''
        print(f"{tid:<14} {status:<11} {age_min:>4.0f}m {n_files:>5}  {title[:45]}{marker}")
        if status == 'HUNG':
            hung_tasks.append((tid, title))
        elif status == 'DEAD':
            dead_tasks.append((tid, title))

    print('-' * 100)
    print(f"Total: {len(tasks)} | " + ' | '.join(f"{k}:{v}" for k, v in sorted(counts.items())))

    if hung_tasks:
        print(f"\nHUNG tasks (process alive but no output for >60m):")
        for tid, title in hung_tasks:
            print(f"  {tid}: {title}")
        print("  → Cross-reference with kanban_show --json for heartbeat detail before blocking.")

    if dead_tasks:
        print(f"\nDEAD tasks (process not found):")
        for tid, title in dead_tasks:
            print(f"  {tid}: {title}")
        print("  → These need kanban_block with reason for re-dispatch.")


if __name__ == '__main__':
    main()
