# Diversity Cap Manual Fallback Pattern (added Cycle #252)

## Problem

`batch_create_tasks.py` applies a 35% thread diversity cap (`DIVERSITY_CAP = 0.35`), which limits how many tasks can be created per research thread. When the queue has 6 active items all in the "other" thread category, the cap produces only 2 tasks even with 8 free workers — wasting 6 worker slots.

The script's output shows:
```
Queue: 6 active items, 6 score >= 30
Workers: 10 free, 12 busy
Uncovered by running tasks: 2
Selected: 2 tasks (diversity cap: 2/thread)
```

The cap calculation: `floor(0.35 × 6) = 2` items max from the "other" thread.

## Workaround

After running `batch_create_tasks.py`, check if the created count is below 50% of free workers. If so, manually create tasks for the remaining items:

```python
# After batch creator runs
import subprocess

# Get next experiment ID
import sqlite3, re
conn = sqlite3.connect('~/.hermes/kanban.db')
rows = conn.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
ids = [int(re.search(r'exp_(\d+)', x[0]).group(1)) for x in rows if re.search(r'exp_(\d+)', x[0])]
next_id = max(ids) + 1 if ids else 1

# Create remaining tasks manually
remaining_items = [...]  # items the batch creator skipped
free_workers = [...]     # workers not assigned by batch creator

for i, item in enumerate(remaining_items):
    exp_id = next_id + i
    worker = free_workers[i % len(free_workers)]
    title = f"exp_{exp_id}: {item['text'][:80]}"
    body = generate_task_body(item['text'])  # reuse batch creator's body generator
    subprocess.run(["hermes", "kanban", "create", title, 
                   "--assignee", worker, "--body", body], 
                  capture_output=True, text=True, timeout=15)
```

## Key Rules

1. **Check created count vs free workers** — if batch creator produces < 50% of free worker slots, fall back to manual
2. **Reuse the batch creator's body generator** — `generate_task_body()` includes GPU instructions, RAG search, model routing
3. **Sequential experiment IDs** — always read from kanban.db, never use exp_AUTO
4. **Dispatch after creation** — `hermes kanban dispatch` may only spawn a subset; spillover is normal

## Alternative: Progressive min-score lowering (recommended first step)

Before manual creation, progressively lower `--min-score` to unlock additional threads. Each threshold step may surface items from threads that were below the previous cutoff. Real example (11 free workers):

```
--min-score 60 → 3 tasks (1 thread: attack)
--min-score 40 → 3 tasks (1 thread: attack)  — no new threads
--min-score 30 → 4 tasks (2 threads: attack + injection)
--min-score 20 → 7 tasks (3 threads: attack + injection + other)
```

Stop lowering when either (a) the created count reaches ~70% of free workers, or (b) the diversity cap becomes the bottleneck (same thread count across multiple threshold drops). At that point, fall back to manual creation for remaining workers.

```bash
# Step 1: try default threshold
python3 ~/.hermes/scripts/batch_create_tasks.py --count 11 --dry-run --min-score 60

# Step 2: lower until threads unlock or cap hits
python3 ~/.hermes/scripts/batch_create_tasks.py --count 11 --dry-run --min-score 30
python3 ~/.hermes/scripts/batch_create_tasks.py --count 11 --dry-run --min-score 20

# Step 3: create for real at the best threshold found
python3 ~/.hermes/scripts/batch_create_tasks.py --count 11 --min-score 20
```

The diversity cap (`floor(0.35 × N)` per thread) still applies at every threshold. When the cap is the binding constraint (not the score threshold), manual creation is the only way to fill remaining worker slots.

## Expected Refill Rounds Per Director Pass

When workers complete in 3-8 minutes (normal for simple experiments), the Director needs multiple refill rounds per pass. Observed pattern from Cycle #246:

| Round | Batch Created | Dispatched | Workers Free After |
|-------|--------------|------------|-------------------|
| 1     | 29           | 29         | 5 (diversity cap) |
| 2     | 16           | 16         | 4 (manual fill)   |
| 3     | 8            | 8          | 3 (manual fill)   |
| 4     | 9            | 9          | 3 (manual fill)   |
| 5     | 12           | 12         | 7 (manual fill)   |
| 6     | 10           | 10         | 6 (manual fill)   |
| 7     | 12           | 12         | 14 (async delay)  |

**Total: ~102 tasks created across 7 rounds in one pass.** The diversity cap (35% per thread) limits each batch to 8-12 tasks even with 20+ free workers. Expect 5-7 refill rounds per pass at 50 workers.

Key timing: `hermes kanban dispatch` returns "Spawned: 0" when the gateway is running — this is NORMAL async delay, not a failure. The gateway picks up ready tasks on its next tick (~60s). Do NOT reassign tasks in this case.

## When to Use

- Queue has 5+ active items but batch creator produces < 3 tasks
- Free workers > 2x created tasks
- All queue items fall into the same thread category (diversity cap hits hard)
