# Bulk Topic Duplication Cleanup (added Cycle #245)

When 3+ topics have 2+ duplicate tasks each, individual reclaim is slow. Use the bulk pattern.

## Problem

Topic duplication wastes workers: 5 tasks investigating "Cross-dataset few-shot adaptation" and 3 investigating "C=50.0 optimal" means 8 of 15 running tasks cover only 2 research questions. The single-duplicate detection (Cycle #185) handles one topic at a time. When duplication is systemic, you need a bulk pass.

## Detection

Group running tasks by normalized topic (the text after `exp_NNN:`):

```python
from collections import defaultdict
import re

topics = defaultdict(list)
for t in running:
    m = re.search(r'exp_(\d+):\s*(.+)', t.get('title', ''))
    if m:
        topic = m.group(2)[:60].lower().strip()
        exp_num = int(m.group(1))
        topics[topic].append((exp_num, t['id']))

for topic, tasks in topics.items():
    if len(tasks) > 1:
        print(f'[{len(tasks)}x] {topic[:70]}')
        for exp_num, tid in tasks:
            print(f'  -> {tid} (exp_{exp_num})')
```

## Cleanup Workflow

For each duplicated topic:
1. Sort tasks by experiment number (ascending)
2. **Keep** the lowest-ID task (oldest, has the original workspace/history)
3. **Reclaim** all others
4. **Block** all reclaimed tasks immediately (prevents re-dispatch on next tick)

```python
import subprocess

for topic, tasks in topics.items():
    if len(tasks) <= 1:
        continue
    tasks.sort()  # ascending by exp_num
    keep = tasks[0][1]
    for exp_num, tid in tasks[1:]:
        subprocess.run(['hermes', 'kanban', 'reclaim', tid],
                      capture_output=True, timeout=10)
        subprocess.run(['hermes', 'kanban', 'block', tid,
                       'duplicate: reclaimed to reduce topic duplication'],
                      capture_output=True, timeout=10)
        print(f'Reclaimed+blocked {tid} (exp_{exp_num}) — duplicate of {keep}')
```

## Why Block After Reclaim

Reclaim alone puts tasks back to `ready`, causing the dispatcher to re-spawn workers on the next tick (see "reclaimed duplicates get re-dispatched" pitfall in main SKILL.md). Blocking after reclaim preserves task history while preventing re-dispatch.

## Real-World Example (Cycle #245)

- **Before**: 15 running tasks, 16 workers
- **Duplication found**: 5× "Cross-dataset few-shot adaptation", 3× "C=50.0 optimal"
- **Action**: Reclaimed 4 + 2 = 6 tasks, blocked all
- **After**: 9 running tasks, ~12 free worker slots
- **Synthesis**: Created task for 2 unsynthesized experiments
- **Result**: Every running task now investigates a distinct hypothesis

## When to Use

- Running task count ≥ 10 AND topic duplication ≥ 2 topics with 2+ tasks each
- At the start of a Director pass, before creating new tasks
- After dispatch spillover produces unexpected duplicates
