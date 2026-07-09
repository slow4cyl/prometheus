#!/usr/bin/env python3
"""
Director queue triage — classify curiosity queue items and detect unsynthesized experiments.

Usage:
    # First dump board state (TIRITH-safe two-step pattern):
    hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json
    hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json

    # Then run triage:
    python3 ~/.hermes/skills/devops/kanban-worker/scripts/director_queue_triage.py

Outputs:
    - Unsynchronized experiments (done but not in self_state.json)
    - Queue item classification (RESOLVED/RUNNING/DONE/NO_SOURCE/UNKNOWN)
    - Coverage scores (HIGH/MED/LOW) against running task titles
    - Worker availability (busy/free count from ps aux)
    - Saturation detection and task creation recommendation
    - Duplicate experiment ID detection
"""
import json, re, os, sys
from collections import defaultdict

SELF_STATE = os.path.expanduser('~/.hermes/self_state.json')
RUNNING_FILE = '/tmp/kanban_running.json'
DONE_FILE = '/tmp/kanban_done.json'

STOP_WORDS = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
              'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
              'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
              'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
              'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
              'which', 'who', 'we', 'you', 'they', 'new', 'other'}

def load_json(path):
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)

def extract_exp_ids(title):
    """Extract experiment IDs from a task title, excluding synthesis tasks."""
    is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
    if is_synth:
        return set()
    ids = set()
    for m in re.finditer(r'exp_(\d+\w*)', title):
        ids.add('exp_%s' % m.group(1))
    return ids

def coverage_score(item_text, running_titles_text):
    """Compute keyword overlap score between item and running task titles."""
    words = set(re.findall(r'\b\w+\b', item_text.lower()))
    keywords = {w for w in words - STOP_WORDS if not re.match(r'^exp_\d+\w*$', w)}
    if not keywords:
        return 0.0
    overlap = sum(1 for w in keywords if w in running_titles_text)
    return overlap / len(keywords)

def main():
    # Load data
    running = load_json(RUNNING_FILE) if os.path.exists(RUNNING_FILE) else []
    done = load_json(DONE_FILE) if os.path.exists(DONE_FILE) else []
    ss = load_json(SELF_STATE)

    # Extract experiment IDs
    running_exp_ids = set()
    for t in running:
        running_exp_ids.update(extract_exp_ids(t.get('title', '')))

    done_exp_ids = set()
    for t in done:
        done_exp_ids.update(extract_exp_ids(t.get('title', '')))

    # SAFE: Always use metrics.experiments_completed_list (handles both int/dict experiments field)
    ss_exp_ids = set()
    completed_list = ss.get('metrics', {}).get('experiments_completed_list', [])
    for e in completed_list:
        if isinstance(e, str):
            m = re.search(r'exp_(\d+\w*)', e)
            if m:
                ss_exp_ids.add('exp_%s' % m.group(1))
        elif isinstance(e, dict):
            eid = e.get('id', '')
            if eid:
                m2 = re.search(r'exp_(\d+\w*)', eid)
                if m2:
                    ss_exp_ids.add('exp_%s' % m2.group(1))
        elif isinstance(e, int):
            # Bare integer entries are valid experiment IDs
            ss_exp_ids.add('exp_%d' % e)

    unsynth = done_exp_ids - ss_exp_ids

    # Coverage scoring
    running_titles_text = ' '.join(t.get('title', '').lower() for t in running)

    # Queue classification
    queue = ss.get('curiosity_queue', [])
    categories = defaultdict(list)

    for i, item in enumerate(queue):
        text = item.get('text', str(item)) if isinstance(item, dict) else str(item)

        if 'RESOLVED' in text.upper() and 'ACTIVE' not in text.upper():
            categories['RESOLVED'].append((i, text, 0.0))
            continue

        source_ids = re.findall(r'exp_(\d+\w*)', text)
        cov = coverage_score(text, running_titles_text)

        if any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
            cat = 'RUNNING'
        elif any(f'exp_{sid}' in ss_exp_ids for sid in source_ids):
            cat = 'DONE'
        elif not source_ids:
            cat = 'NO_SOURCE'
        else:
            cat = 'UNKNOWN'

        categories[cat].append((i, text, cov))

    # Output
    print("=" * 70)
    print("DIRECTOR QUEUE TRIAGE")
    print("=" * 70)

    print(f"\nBoard: {len(running)} running, {len(done)} done")
    print(f"Experiments: {len(done_exp_ids)} done IDs, {len(ss_exp_ids)} in self_state")
    print(f"Unsynthesized: {len(unsynth)}")
    if unsynth:
        def sort_key(x):
            m = re.search(r'(\d+)', x)
            return int(m.group(1)) if m else 0
        sorted_ids = sorted(unsynth, key=sort_key)
        print("  IDs: %s" % ', '.join(sorted_ids))

    print(f"\nQueue: {len(queue)} items")
    for cat in ['NO_SOURCE', 'RUNNING', 'DONE', 'UNKNOWN', 'RESOLVED']:
        items = categories.get(cat, [])
        if not items:
            continue
        print(f"\n--- {cat} ({len(items)} items) ---")
        for idx, text, cov in items:
            cov_label = 'HIGH' if cov > 0.3 else ('MED' if cov > 0.15 else 'LOW')
            print(f"  [{idx:2d}] (cov={cov:.2f}/{cov_label}) {text[:90]}")

    # Worker availability (TIRITH-safe: subprocess, not pipe)
    import subprocess
    try:
        ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        busy_workers = set()
        for line in ps_result.stdout.split('\n'):
            m = re.search(r'prometheus-worker-(\d+)', line)
            if m:
                busy_workers.add(int(m.group(1)))
        all_workers = set(range(1, 51))  # workers 1-22
        free_workers = sorted(all_workers - busy_workers)
    except Exception:
        busy_workers = set()
        free_workers = []

    # Summary
    no_source_low = [(i, t, c) for i, t, c in categories.get('NO_SOURCE', []) if c < 0.15]
    done_active = [(i, t, c) for i, t, c in categories.get('DONE', []) if c < 0.3]
    print(f"\n--- RECOMMENDATIONS ---")
    print(f"NO_SOURCE with LOW coverage (candidates for new tasks): {len(no_source_low)}")
    print(f"DONE with LOW coverage (follow-up candidates): {len(done_active)}")
    print(f"RUNNING (skip — will resolve naturally): {len(categories.get('RUNNING', []))}")
    print(f"RESOLVED (skip): {len(categories.get('RESOLVED', []))}")
    print(f"Workers: {len(busy_workers)} busy, {len(free_workers)} free {free_workers if free_workers else '(none)'}")
    print(f"Tasks: {len(running)} running, saturation={'YES' if len(running) >= 7 else 'no'}")

    # Decision helper
    if len(running) >= 7:
        print(f"\nSATURATION MODE: Do NOT create experiment tasks. Create synthesis if unsynthesized > 2.")
    elif no_source_low:
        print(f"\nCREATE TASKS: {len(no_source_low)} NO_SOURCE items with LOW coverage are available.")
    else:
        print(f"\nNO ACTION: All queue items are covered by running tasks.")

    # Duplicate experiment ID detection
    exp_to_tasks = defaultdict(list)
    for t in running:
        title = t.get('title', '')
        for eid in extract_exp_ids(title):
            exp_to_tasks[eid].append(t['id'])
    dupes = {eid: tids for eid, tids in exp_to_tasks.items() if len(tids) > 1}
    if dupes:
        print(f"\n--- DUPLICATE EXPERIMENT IDs ({len(dupes)} found) ---")
        for eid, tids in sorted(dupes.items()):
            print("  %s: %s" % (eid, ', '.join(tids)))
        print("  Reclaim newer duplicate to prevent wasted workers")

if __name__ == '__main__':
    main()
