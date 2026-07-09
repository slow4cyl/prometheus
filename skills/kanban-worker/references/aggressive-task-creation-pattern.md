# Aggressive Task Creation Pattern (Director)

## Goal

When free_workers ≥ 5 AND queue has ≥3 uncovered items, create tasks for ALL uncovered items — not just the top 2-3. Idle workers = wasted capacity.

## Diversity Cap

The `batch_create_tasks.py` script applies a 35% per-thread diversity cap: `floor(0.35 × N)` tasks per thread, where N is the total tasks to create.

### Example Calculation

With 10 tasks to create across 3 threads:
- Thread A (5 items): cap = floor(0.35 × 10) = 3 tasks
- Thread B (3 items): cap = floor(0.35 × 10) = 3 tasks  
- Thread C (2 items): cap = floor(0.35 × 10) = 3 tasks
- Total: 8 tasks (2 slots unfilled because Thread C only has 2 items)

### When the Cap Blocks Aggressive Creation

With few active threads, the cap severely limits task count:

| Free Workers | Active Threads | Cap/Thread | Max Created |
|:---|:---|:---|:---|
| 10 | 2 | 1/thread | 2 |
| 10 | 3 | 1/thread | 3 |
| 10 | 5 | 1/thread | 5 |

In Director Pass 589 (2026-06-02): 4 free workers, 2 active threads → script created only 2 tasks.

## Mitigation

1. **Run batch script first** for scoring and dedup
2. **Manually create remaining tasks** for uncovered NO_SOURCE queue items
3. **Use lowest-coverage items** (coverage < 0.15 against running tasks)

### Manual Creation Pattern

```python
# Get next experiment ID
import sqlite3, re
c = sqlite3.connect('~/.hermes/kanban.db')
r = c.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
ids = [int(re.search(r'exp_(\d+)', x[0]).group(1)) for x in r if re.search(r'exp_(\d+)', x[0])]
next_id = max(ids) + 1

# Create task
hermes kanban create "exp_{next_id}: <hypothesis>" \
  --assignee prometheus-worker-N \
  --body "<task body with GPU/RAG instructions>"
```

## Anti-Patterns

- **Creating only 2-3 tasks when 10+ workers are free** — the flowchart's "Assign to free workers only" means "fill available slots," not "create a few and stop"
- **Trusting the batch script's "Selected: N" output** — always compare against free worker count
- **Skipping topic duplicate detection** — batch script can create tasks for topics already covered by running experiments (see `batch-creator-coverage-pitfall.md`)
