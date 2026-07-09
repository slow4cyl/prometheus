# Pre-Creation Topic Overlap Check (Cycle #251)

## Problem

`batch_create_tasks.py --dry-run` proposed 12 tasks, but 5 had exact or near-exact title overlap with already-running experiments:

| Proposed Task | Running Experiment | Overlap Score |
|---|---|---|
| "standalone TF-IDF+LR wins at 100+ domains" | exp_2539 (same topic) | 1.00 |
| "ensemble disagreement AUC 0.84-0.97" | exp_2532 (same topic) | 1.00 |
| "direct LR 36x faster but training 4.4x longer" | exp_2561 (same topic) | 1.00 |
| "oracle gap 0.0005 for ensemble sizing" | exp_2544 (same topic) | 1.00 |
| "dynamic ensemble sizing CONFIRMED" | exp_2544 (partial) | 0.50 |

The batch creator's internal Jaccard check compares queue item text against running task titles, but the proposed tasks are NEW items generated from queue scoring — the overlap is between the proposed task's topic and the running experiment's topic, not the queue item's source text.

## Detection Pattern

After `--dry-run`, extract topic keywords from each proposed task title and check against running task titles:

```python
import json, re

running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '').lower() for t in running]

proposed_titles = [
    "standalone TF-IDF+LR wins at 100+ domains",
    "ensemble disagreement AUC 0.84-0.97",
    # ... from dry-run output
]

for i, prop in enumerate(proposed_titles):
    prop_words = set(w for w in prop.lower().split() if len(w) > 3)
    for rt in running_titles:
        rt_words = set(w for w in rt.split() if len(w) > 3)
        overlap = len(prop_words & rt_words) / max(len(prop_words), 1)
        if overlap > 0.3:
            print(f'OVERLAP [{i}]: "{prop[:50]}" vs "{rt[:60]}" (score={overlap:.2f})')
            break
    else:
        print(f'  OK [{i}]: {prop[:60]}')
```

## Threshold

- **>0.3 keyword overlap**: Skip the proposed task (near-duplicate)
- **1.0 exact match**: Definitely skip
- **0.3-0.5**: Likely duplicate, skip unless the question scope is genuinely different

## Fix Applied in Cycle #251

Manually created 13 tasks after filtering out 5 overlapping proposals. Used the keyword overlap check to select only genuinely unique topics from the queue.

## Root Cause

The batch creator's coverage check operates at the queue-item level (is this queue item already covered by a running experiment?), not at the proposed-task level (does this proposed task duplicate a running experiment?). These are different questions — a queue item can be "uncovered" (its source experiment isn't running) while its topic is already being investigated by a different running experiment.

## Prevention

Before creating tasks from batch creator output:
1. Run `--dry-run` first
2. Extract topic keywords from each proposed task title
3. Check against running task titles using keyword overlap
4. Skip any proposed task with >30% keyword overlap against any running title
5. Only create tasks for genuinely unique topics
