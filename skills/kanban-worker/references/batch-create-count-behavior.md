# batch_create_tasks.py Usage Patterns

## Count Parameter Behavior

The `--count` parameter requests N tasks but the script applies internal constraints:

- **Diversity cap**: Scales with count (default: 1/thread for small counts, up to 4/thread for large counts)
- **Score threshold**: `--min-score` filters queue items (default: 60, lower for more tasks)
- **Actual output**: Typically 70-80% of requested count

### Observed Behavior (Cycle 278 + June 4 2026)

| Requested | Created | Ratio | Notes |
|-----------|---------|-------|-------|
| 5 | 4 | 80% | Diversity cap: 1/thread |
| 8 | 8 | 100% | Diversity cap: 2/thread, many uncovered items |
| 12 | 9 | 75% | Diversity cap: 4/thread |
| 18 | 10 | 56% | Diversity cap: 6/thread, but only 10 uncovered items (coverage ceiling) |
| 25 | 15-18 | 60-72% | Diversity cap: 8/thread |

**Key insight:** Output is limited by BOTH diversity cap AND coverage. Even with `--count 18`, only 10 tasks were created because the batch creator found only 10 uncovered items (queue mostly covered by running tasks). The diversity cap is the upper bound; coverage is the practical limit.

### Recommendation

Request 1.5x the free worker count to achieve near-full utilization:

```bash
# For 12 free workers:
python3 ~/.hermes/scripts/batch_create_tasks.py --count 18 --min-score 30

# Verify actual creation count in output before dispatching
```

## Dispatch Behavior

After creating tasks, `hermes kanban dispatch` may report "Spawned: 0". This is NORMAL — the scheduler picks up ready tasks on the next tick (typically within 1-2 minutes).

### Verification Pattern

```bash
# 1. Create tasks
python3 ~/.hermes/scripts/batch_create_tasks.py --count 12 --min-score 30

# 2. Dispatch (may show 0 spawned)
hermes kanban dispatch

# 3. Verify after 1-2 minutes
hermes kanban list --status running --json | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
```

## Thread Diversity

The script groups queue items by thread (keyword-based classification). The diversity cap limits how many tasks can come from the same thread:

- `--count 5`: 1 task/thread max
- `--count 12`: 4 tasks/thread max
- `--count 20`: ~6 tasks/thread max

This prevents monopolization by a single research thread (e.g., 10 TF-IDF experiments when other threads have uncovered items).

## Diversity Cap vs Free Workers

When the diversity cap limits created tasks below the free worker count (e.g., 4 created for 6 free workers), do NOT manually force-create extra tasks to fill slots. Instead, run the queue coverage check (see `director-queue-coverage-check.md`):

```python
# After batch_create_tasks.py, check if any uncovered items remain
uncovered = [item for item in queue if coverage_score(item, running_titles) < 0.15]
if not uncovered:
    # Queue is converged — idle workers are correct, no action needed
    pass
else:
    # Create additional tasks manually for uncovered items
```

The diversity cap exists to prevent thread monopolization. If the queue is converged (all items covered), idle workers are the correct outcome. See `queue-convergence-detection.md` for the full pattern.

## Score Threshold

- `--min-score 60` (default): Conservative, only high-priority items
- `--min-score 30`: Moderate, includes medium-priority items
- `--min-score 10`: Aggressive, includes most queue items

Use lower thresholds when workers are idle and queue items are genuinely uncovered.
