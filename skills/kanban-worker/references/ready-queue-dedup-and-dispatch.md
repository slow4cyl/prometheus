# Ready Queue Dedup and Dispatch Patterns (added Cycle #229)

## Problem

Ready tasks accumulate from previous Director passes. Some duplicate running experiments (wasting workers), and some have wrong assignee naming ("worker-N" instead of "prometheus-worker-N") causing silent dispatch skips.

## Ready Queue Duplicate Detection

Before dispatching, detect which ready tasks duplicate running experiments using keyword-overlap scoring:

```python
import json, re

running = json.load(open('/tmp/kanban_running.json'))
ready = json.load(open('/tmp/kanban_ready.json'))

running_topics = {}
for t in running:
    m = re.search(r'exp_\d+:\s*(.+)', t.get('title', ''))
    if m: running_topics[t['id']] = m.group(1).lower()

for t in ready:
    m = re.search(r'exp_\d+:\s*(.+)', t.get('title', ''))
    topic = m.group(1).lower() if m else t.get('title', '').lower()
    is_dup = False
    for rtid, rtopic in running_topics.items():
        overlap = set(topic.split()).intersection(set(rtopic.split()))
        if len(overlap) >= 3:
            is_dup = True
            break
    status = 'DUPLICATE' if is_dup else 'INDEPENDENT'
    print(f'{t["id"]}: {topic[:60]} -> {status}')
```

**Threshold**: 3+ shared words = DUPLICATE. Adjust for your domain (regulatory domains have many shared terms).

**In Cycle #229**: 10 ready tasks → 4 independent, 6 duplicates. Dispatching only independent tasks prevented 6 wasted worker slots.

## Wrong Assignee Names

Ready tasks created by previous Director passes may use "worker-N" instead of "prometheus-worker-N". The scheduler skips these silently.

**Detection**: After `hermes kanban dispatch`, check for "Spawned: 0" with tasks listed under "Skipped (non-spawnable assignee)".

**Fix**: Reassign to correct profiles before dispatching:
```bash
hermes kanban assign <task_id> prometheus-worker-N
```
Then re-run `hermes kanban dispatch`.

**In Cycle #229**: All 10 ready tasks had "worker-N" assignees. After reassigning 4 independent tasks to free workers (8, 18, 20, 21), dispatch succeeded.

## Dispatch Verification

After dispatch, verify tasks actually moved to running:
```bash
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running2.json
```
Then check specific task IDs appear in the running list. The scheduler may take a tick to process reassignments.

## Free Worker Detection

When deciding which ready tasks to dispatch, identify free workers first:
```python
import subprocess, re
result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
busy = set()
for line in result.stdout.split('\n'):
    m = re.search(r'prometheus-worker-(\d+)', line)
    if m: busy.add(int(m.group(1)))
free = set(range(1, 23)) - busy
print(f'Free workers: {sorted(free)}')
```

Assign independent ready tasks to free workers only. Never assign 2 tasks to the same worker.
