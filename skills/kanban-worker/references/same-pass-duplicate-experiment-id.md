# Same-Pass Duplicate Experiment ID (added June 3, 2026)

## Problem

Two tasks created in the **same Director pass** receive the same experiment ID prefix (`exp_NNN`) but contain completely different hypotheses. This is distinct from:

- **Variant 1** (`director-experiment-id-conflict-pitfall.md`): New task conflicts with EXISTING running task
- **Variant 2** (`director-experiment-id-conflict-pitfall.md`): New task conflicts with EXISTING completed task

This variant happens when task creation doesn't use sequential IDs or when the batch creator and manual creation overlap.

## Example (June 3, 2026)

- **Task** t_5d02dbbb: `exp_2964: All vocab alignment methods identical at 100% — is this an artifact`
- **Task** t_fe1e3723: `exp_2964: Anti-deference 0% on mimo vs 37.5% on other models — what model proper`

Same ID (`exp_2964`), completely different topics. Both were running simultaneously.

## Detection

During Director pass, after collecting board state:

```python
import json, re
from collections import Counter

with open('/tmp/kanban_running.json') as f:
    running = json.load(f)

# Extract experiment IDs
exp_ids = []
for t in running:
    title = t.get('title', '')
    m = re.search(r'exp_(\d+)', title)
    if m:
        exp_ids.append(f'exp_{m.group(1)}')

# Find duplicates
dupes = {k: v for k, v in Counter(exp_ids).items() if v > 1}
if dupes:
    print(f"DUPLICATE EXPERIMENT IDs: {dupes}")
    # For each duplicate, check if topics differ
    for exp_id, count in dupes.items():
        tasks = [t for t in running if exp_id in t.get('title', '')]
        topics = [re.search(r'exp_\d+:\s*(.+)', t['title']).group(1)[:50] 
                  for t in tasks if re.search(r'exp_\d+:\s*(.+)', t['title'])]
        print(f"  {exp_id}: {topics}")
```

## Fix

1. **BLOCK the newer duplicate** (not reclaim — reclaim causes re-dispatch):
   ```bash
   hermes kanban block <newer_task_id> "Duplicate experiment ID exp_NNN — original task <original_id> already running this ID"
   ```

2. **Kill the worker process** working on the blocked task:
   ```bash
   # Find the worker process
   ps aux | grep "<blocked_task_id>" | grep -v grep
   # Kill it
   kill <pid>
   ```

3. **Log the collision** in Director audit log for pattern tracking

## Prevention

1. **ALWAYS use the max-ID approach** from `director-experiment-id-conflict-pitfall.md`:
   ```python
   next_exp_id = max(all_exp_ids) + 1 if all_exp_ids else 1
   ```

2. **Never create tasks with hardcoded IDs** — always query kanban.db for the current max

3. **When using batch_create_tasks.py**, verify it uses the same ID generation logic

## Related Pitfalls

- `director-experiment-id-conflict-pitfall.md` — Variants 1 & 2 (conflict with existing tasks)
- `same-id-different-topic-collision.md` — Same ID, different topics (detection pattern)
- This file: Variant 3 — Same-pass duplicate (new tasks get same ID)
