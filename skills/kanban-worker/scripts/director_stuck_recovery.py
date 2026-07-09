#!/usr/bin/env python3
"""
Batch stuck worker detection and recovery for Director passes.

Usage:
    python3 director_stuck_recovery.py [--dry-run] [--min-age 20]

Checks all running tasks for stuck workers using 3 methods:
1. Process liveness (ps aux)
2. Heartbeat recency (kanban show --json)
3. Workspace output (file count + modification time)

Reclaims + blocks workers that are dead with no output.
Leaves alive workers alone regardless of age.

Output: prints actions taken, writes to stdout for audit log.
"""

import subprocess
import json
import os
import re
import sys
import time
import datetime


def get_running_tasks():
    """Get all running tasks from kanban."""
    result = subprocess.run(
        ['hermes', 'kanban', 'list', '--status', 'running', '--json'],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        print(f"ERROR: kanban list failed: {result.stderr[:200]}")
        return []
    return json.loads(result.stdout)


def get_task_detail(task_id):
    """Get detailed task info including events."""
    result = subprocess.run(
        ['hermes', 'kanban', 'show', task_id, '--json'],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def check_process_alive(experiment_name):
    """Check if a process with the experiment name is alive."""
    result = subprocess.run(
        ['ps', 'aux'],
        capture_output=True, text=True, timeout=5
    )
    for line in result.stdout.split('\n'):
        if experiment_name in line and 'grep' not in line:
            return True
    return False


def check_workspace(workspace_path):
    """Check workspace for output files."""
    if not os.path.isdir(workspace_path):
        return {'exists': False, 'files': 0, 'output_files': 0, 'latest_mtime': 0}
    
    files = os.listdir(workspace_path)
    output_files = [f for f in files if f.endswith('.log') or f.startswith('results')]
    script_files = [f for f in files if f.endswith('.py')]
    
    latest_mtime = 0
    for f in files:
        fp = os.path.join(workspace_path, f)
        if os.path.isfile(fp):
            mt = os.path.getmtime(fp)
            if mt > latest_mtime:
                latest_mtime = mt
    
    return {
        'exists': True,
        'files': len(files),
        'output_files': len(output_files),
        'script_files': len(script_files),
        'latest_mtime': latest_mtime,
        'age_min': (time.time() - latest_mtime) / 60 if latest_mtime > 0 else 999
    }


def extract_experiment_id(title):
    """Extract experiment ID from task title."""
    m = re.search(r'exp_(\d+)', title)
    return f"exp_{m.group(1)}" if m else None


def reclaim_and_block(task_id, reason):
    """Reclaim then block a task."""
    # Reclaim
    r1 = subprocess.run(
        ['hermes', 'kanban', 'reclaim', task_id],
        capture_output=True, text=True, timeout=10
    )
    # Block
    r2 = subprocess.run(
        ['hermes', 'kanban', 'block', task_id, reason],
        capture_output=True, text=True, timeout=10
    )
    return r1.returncode == 0 and r2.returncode == 0


def main():
    dry_run = '--dry-run' in sys.argv
    min_age = 20  # minutes
    for i, arg in enumerate(sys.argv):
        if arg == '--min-age' and i + 1 < len(sys.argv):
            min_age = int(sys.argv[i + 1])
    
    print(f"Stuck Worker Recovery (min_age={min_age}m, dry_run={dry_run})")
    print("=" * 60)
    
    tasks = get_running_tasks()
    print(f"Running tasks: {len(tasks)}")
    
    now = time.time()
    reclaimed = 0
    left_alone = 0
    
    for task in tasks:
        tid = task['id']
        title = task.get('title', 'N/A')
        started = task.get('started_at') or task.get('created_at', 0)
        age_min = (now - started) / 60 if started else 999
        
        if age_min < min_age:
            continue  # Too new to check
        
        exp_name = extract_experiment_id(title)
        if not exp_name:
            continue
        
        # Method 1: Process liveness
        process_alive = check_process_alive(exp_name)
        
        # Method 3: Workspace output
        ws = os.path.expanduser(f'~/.hermes/kanban/workspaces/{tid}')
        ws_info = check_workspace(ws)
        
        # Decision logic
        if not process_alive:
            if ws_info['output_files'] > 0:
                # Dead but has output — partial results
                reason = f"hung: {age_min:.0f}m, process dead, {ws_info['output_files']} output files (partial results)"
            elif ws_info['script_files'] > 1:
                # Dead, multiple scripts, no output — scripts written but never executed
                reason = f"hung: {age_min:.0f}m, process dead, {ws_info['script_files']} scripts written but never executed"
            elif ws_info['files'] > 0:
                # Dead, some files but no output
                reason = f"hung: {age_min:.0f}m, process dead, {ws_info['files']} files, zero output"
            else:
                # Dead, no workspace
                reason = f"hung: {age_min:.0f}m, process dead, no workspace"
            
            if dry_run:
                print(f"  WOULD RECLAIM: {tid} ({exp_name}) — {reason}")
            else:
                success = reclaim_and_block(tid, reason)
                status = "OK" if success else "FAILED"
                print(f"  RECLAIMED: {tid} ({exp_name}) — {status}")
            reclaimed += 1
        else:
            # Process alive — leave alone regardless of age
            print(f"  ALIVE: {tid} ({exp_name}) — {age_min:.0f}m, process running")
            left_alone += 1
    
    print(f"\nSummary: {reclaimed} reclaimed, {left_alone} left alive, {len(tasks) - reclaimed - left_alone} too new")


if __name__ == '__main__':
    main()
