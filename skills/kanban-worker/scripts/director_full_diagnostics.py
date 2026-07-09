#!/usr/bin/env python3
"""
Director Full Diagnostics — single script for the start of every Director pass.

Replaces 8+ ad-hoc diagnostic scripts with one consolidated check.
Run this FIRST, then act on the output.

Runs:
1. Dump board state (running/done/ready/blocked) to /tmp/kanban_*.json
2. Check worker availability via ps aux (busy vs free, 1-22)
3. Detect zombies (process alive but not in running list)
4. Detect status desync (in running list but actually done via kanban show)
5. Check for unsynthesized experiments (done_exp_ids minus ss_exp_ids)
6. Output summary

Usage:
    python3 scripts/director_full_diagnostics.py                  # full check
    python3 scripts/director_full_diagnostics.py --fast            # skip desync check (saves N API calls)
    python3 scripts/director_full_diagnostics.py --no-desync       # same as --fast

Output:
    - Prints summary to stdout
    - Writes JSON files to /tmp/kanban_{running,done,ready,blocked}.json

TIRITH-safe: No piping to python3. All hermes CLI output written to files first.
"""

import subprocess
import json
import re
import os
import sys
from datetime import datetime, timezone


def run_cmd(cmd, timeout=30):
    """Run a command and return stdout. TIRITH-safe: no pipes."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout
    except Exception as e:
        return f"ERROR: {e}"


def dump_board_state():
    """Dump board state to /tmp/kanban_*.json files."""
    for status in ['running', 'done', 'ready', 'blocked']:
        output = run_cmd(['hermes', 'kanban', 'list', '--status', status, '--json'])
        path = f'/tmp/kanban_{status}.json'
        with open(path, 'w') as f:
            f.write(output)
    print("[1/5] Board state dumped to /tmp/kanban_*.json")


def check_worker_availability():
    """Check which workers are busy vs free via ps aux."""
    result = run_cmd(['ps', 'aux'])

    worker_tasks = {}
    for line in result.split('\n'):
        if 'hermes' in line and 'kanban task' in line:
            m_task = re.search(r'kanban task (t_\w+)', line)
            m_worker = re.search(r'prometheus-worker-(\d+)', line)
            if m_task and m_worker:
                tid = m_task.group(1)
                worker = int(m_worker.group(1))
                if worker not in worker_tasks:
                    worker_tasks[worker] = []
                worker_tasks[worker].append(tid)

    all_workers = set(range(1, 51))
    busy = set(worker_tasks.keys())
    free = all_workers - busy

    return {
        'busy_workers': sorted(busy),
        'free_workers': sorted(free),
        'busy_count': len(busy),
        'free_count': len(free),
        'worker_tasks': {k: v for k, v in worker_tasks.items()}
    }


def detect_zombies():
    """Detect zombies: process alive with 'kanban task' but not in running list."""
    running = json.load(open('/tmp/kanban_running.json'))
    running_ids = {t['id'] for t in running}

    result = run_cmd(['ps', 'aux'])
    visible_tasks = set()
    for line in result.split('\n'):
        if 'hermes' in line and 'kanban task' in line:
            m_task = re.search(r'kanban task (t_\w+)', line)
            if m_task:
                visible_tasks.add(m_task.group(1))

    # Also check for prometheus-synthesis (not prometheus-worker-N)
    synth_running = 'prometheus-synthesis' in result

    zombies = visible_tasks - running_ids
    return {
        'zombie_count': len(zombies),
        'zombie_ids': sorted(zombies),
        'synthesis_process_alive': synth_running
    }


def detect_status_desync():
    """Detect status desync: task in running list but actually done.
    
    WARNING: This calls kanban show for EVERY running task (N API calls).
    Use --fast/--no-desync to skip when the running count is high.
    """
    running = json.load(open('/tmp/kanban_running.json'))
    desynced = []
    verified_running = 0

    for t in running:
        tid = t['id']
        output = run_cmd(['hermes', 'kanban', 'show', tid, '--json'])
        try:
            d = json.loads(output)
            status = d.get('task', {}).get('status', '?')
            if status == 'done':
                desynced.append({
                    'id': tid,
                    'title': t.get('title', '?')[:60]
                })
            elif status == 'running':
                verified_running += 1
        except:
            pass

    return {
        'desync_count': len(desynced),
        'desynced': desynced,
        'verified_running': verified_running
    }


def check_unsynthesized():
    """Check for unsynthesized experiments (Method A: done_exp_ids minus ss_exp_ids)."""
    done = json.load(open('/tmp/kanban_done.json'))
    done_exp_ids = set()
    for t in done:
        title = t.get('title', '')
        # Exclude synthesis/consolidation tasks — they mention experiment IDs as subjects
        is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
        if not is_synth:
            for m in re.finditer(r'exp_(\d+\w*)', title):
                done_exp_ids.add(f'exp_{m.group(1)}')

    ss_path = os.path.expanduser('~/.hermes/self_state.json')
    ss = json.load(open(ss_path))
    ss_exp_ids = set()
    for e in ss.get('experiments', {}).get('completed', []):
        if isinstance(e, dict):
            eid = e.get('id', '')
            if eid:
                ss_exp_ids.add(eid)
        elif isinstance(e, str):
            m = re.search(r'exp_(\d+\w*)', e)
            if m:
                ss_exp_ids.add(f'exp_{m.group(1)}')

    unsynth = done_exp_ids - ss_exp_ids
    # Sort numerically (handles exp_104b etc.)
    def sort_key(x):
        m = re.search(r'(\d+)', x)
        return int(m.group(1)) if m else 0
    sorted_unsynth = sorted(unsynth, key=sort_key)

    return {
        'done_count': len(done_exp_ids),
        'ss_count': len(ss_exp_ids),
        'unsynth_count': len(sorted_unsynth),
        'unsynth_ids': sorted_unsynth
    }


def main():
    fast_mode = '--fast' in sys.argv or '--no-desync' in sys.argv

    print(f"=== DIRECTOR FULL DIAGNOSTICS — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===")
    if fast_mode:
        print("    (fast mode: skipping status desync check)")
    print()

    # 1. Dump board state
    dump_board_state()

    # 2. Check worker availability
    workers = check_worker_availability()
    print(f"[2/5] WORKER AVAILABILITY:")
    print(f"       Busy: {workers['busy_count']} workers {workers['busy_workers']}")
    print(f"       Free: {workers['free_count']} workers {workers['free_workers']}")

    # 3. Detect zombies
    zombies = detect_zombies()
    print(f"\n[3/5] ZOMBIES: {zombies['zombie_count']}", end="")
    if zombies['zombie_ids']:
        print(f" {zombies['zombie_ids']}")
    else:
        print(" (none)")
    print(f"       Synthesis process alive: {zombies['synthesis_process_alive']}")

    # 4. Status desync (optional — expensive, N API calls)
    if fast_mode:
        print(f"\n[4/5] STATUS DESYNC: skipped (--fast mode)")
        desync = {'desync_count': '?', 'verified_running': '?'}
    else:
        desync = detect_status_desync()
        print(f"\n[4/5] STATUS DESYNC: {desync['desync_count']} tasks actually done but listed as running")
        if desync['desynced']:
            for d in desync['desynced']:
                print(f"       {d['id']}: {d['title']}")
        print(f"       Verified running: {desync['verified_running']}")

    # 5. Unsusnthesized experiments
    unsynth = check_unsynthesized()
    print(f"\n[5/5] UNSYNTHESIZED: {unsynth['unsynth_count']} experiments")
    print(f"       Done task IDs with exp_*: {unsynth['done_count']}")
    print(f"       Self-state experiment IDs: {unsynth['ss_count']}")
    if unsynth['unsynth_ids']:
        for eid in unsynth['unsynth_ids'][:10]:
            print(f"       {eid}")
        if unsynth['unsynth_count'] > 10:
            print(f"       ... and {unsynth['unsynth_count'] - 10} more")

    # Summary
    running = json.load(open('/tmp/kanban_running.json'))
    print(f"\n=== SUMMARY ===")
    print(f"Running tasks:      {len(running)}")
    print(f"Free workers:       {workers['free_count']}")
    print(f"Unsynthesized:      {unsynth['unsynth_count']}")
    print(f"Zombies:            {zombies['zombie_count']}")
    if not fast_mode:
        print(f"Status desync:      {desync['desync_count']}")
    print(f"Synthesis running:  {zombies['synthesis_process_alive']}")

    # Decision hints
    print(f"\n=== DECISION HINTS ===")
    verified = desync.get('verified_running', len(running))
    if isinstance(verified, int) and verified >= 7:
        print(f"SATURATION: {verified} verified running tasks >= 7 threshold")
        print(f"  → SYNTHESIZE only, skip experiment task creation")
        if workers['free_count'] >= 3 and unsynth['unsynth_count'] >= 3:
            print(f"  → EXCEPTION: {workers['free_count']} free workers + {unsynth['unsynth_count']} unsynth → create SYNTHESIS task")
    elif isinstance(verified, int) and workers['free_count'] > 0:
        print(f"BELOW SATURATION: {verified} verified running, {workers['free_count']} free workers")
        print(f"  → CREATE tasks for free workers if queue has high-value items")
    if unsynth['unsynth_count'] >= 3:
        print(f"SYNTHESIS NEEDED: {unsynth['unsynth_count']} unsynthesized experiments")
    if zombies['zombie_count'] > 0:
        print(f"ZOMBIE CLEANUP: {zombies['zombie_count']} zombie processes to kill")


if __name__ == '__main__':
    main()
