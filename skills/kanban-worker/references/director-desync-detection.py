#!/usr/bin/env python3
"""
Detect status desync between kanban list and actual task state.

Cross-references three data sources:
1. kanban list --status running (what the board says)
2. kanban list --status done (ground truth for completed tasks)
3. ps aux (ground truth for live worker processes)

Returns: list of desync'd tasks (shown as running but actually done or workerless).

Usage (Director pass):
  hermes kanban list --status running --json > /tmp/kanban_running.json
  hermes kanban list --status done --json > /tmp/kanban_done.json
  python3 references/director-desync-detection.py
"""

import json, subprocess, re, sys, os


def get_running_workers():
    """Extract worker profile numbers and their task IDs from ps aux."""
    result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
    workers = set()
    worker_tasks = {}  # task_id -> worker_number
    
    for line in result.stdout.split('\n'):
        if 'hermes' not in line:
            continue
        
        # Match prometheus-worker-N profiles
        m_worker = re.search(r'prometheus-worker-(\d+)', line)
        m_task = re.search(r'kanban task (t_\w+)', line)
        
        if m_worker and m_task:
            wnum = int(m_worker.group(1))
            tid = m_task.group(1)
            workers.add(wnum)
            worker_tasks[tid] = wnum
        
        # Also match prometheus-synthesis profile
        if 'prometheus-synthesis' in line and m_task:
            worker_tasks[m_task.group(1)] = 'synthesis'
    
    return workers, worker_tasks


def detect_desync(running_path='/tmp/kanban_running.json', 
                   done_path='/tmp/kanban_done.json'):
    """
    Detect tasks that appear running but are actually done or workerless.
    
    Returns dict with:
      - desync_done: tasks in running list that are actually done
      - desync_workerless: tasks in running list with no worker process
      - free_workers: worker numbers with no assigned task
      - true_running: count of genuinely running tasks
    """
    running = json.load(open(running_path))
    done = json.load(open(done_path))
    done_ids = {t['id'] for t in done}
    
    all_workers, worker_tasks = get_running_workers()
    all_worker_set = set(range(1, 23))  # workers 1-22
    
    desync_done = []
    desync_workerless = []
    
    for t in running:
        tid = t['id']
        title = t.get('title', '?')
        
        # Check 1: is the task actually done?
        if tid in done_ids:
            desync_done.append({'id': tid, 'title': title, 'reason': 'in done list'})
            continue
        
        # Check 2: does it have a worker process?
        if tid not in worker_tasks:
            desync_workerless.append({'id': tid, 'title': title, 'reason': 'no worker process'})
    
    true_running = len(running) - len(desync_done) - len(desync_workerless)
    occupied_workers = set(worker_tasks.values())
    free_workers = all_worker_set - all_workers
    
    return {
        'desync_done': desync_done,
        'desync_workerless': desync_workerless,
        'free_workers': sorted(free_workers),
        'true_running': true_running,
        'listed_running': len(running),
        'total_workers': len(all_workers),
    }


if __name__ == '__main__':
    result = detect_desync()
    
    print(f"Listed running: {result['listed_running']}")
    print(f"True running:   {result['true_running']}")
    print(f"Active workers: {result['total_workers']}")
    print(f"Free workers:   {result['free_workers']}")
    
    if result['desync_done']:
        print(f"\nDESYNC (actually done): {len(result['desync_done'])}")
        for d in result['desync_done']:
            print(f"  {d['id']}: {d['title'][:60]}")
    
    if result['desync_workerless']:
        print(f"\nWORKERLESS (no process): {len(result['desync_workerless'])}")
        for d in result['desync_workerless']:
            print(f"  {d['id']}: {d['title'][:60]}")
    
    if not result['desync_done'] and not result['desync_workerless']:
        print("\nNo desync detected — board state is consistent.")
