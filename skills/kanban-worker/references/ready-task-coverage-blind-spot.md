# Batch Creator Ready-Task Coverage Blind Spot

**Date:** Cycle #245 (June 3, 2026)
**Symptom:** `batch_create_tasks.py` reports "Uncovered by running tasks: 0" when run against `status='running'` tasks, but manual analysis against BOTH running AND ready tasks shows 29 uncovered items.
**Root cause:** The batch creator's coverage check queries only `status='running'` tasks. Tasks that are `status='ready'` (assigned but not yet dispatched) are invisible to the coverage check.

## Why This Matters

When the Director creates tasks and assigns them, those tasks sit in `ready` status until the dispatch daemon picks them up. During this window:
- The batch creator sees them as "not running" → doesn't count them as coverage
- Queue items overlapping with ready tasks appear "uncovered" → get created
- After dispatch, both the ready task AND the new task run → duplicate work

## The Correct Coverage Check

When evaluating queue item coverage, check against BOTH running AND ready tasks:

```python
# WRONG: only checks running tasks
running = [r[0] for r in db.execute(
    "SELECT title FROM tasks WHERE status='running'").fetchall()]

# CORRECT: checks running AND ready tasks
running = [r[0] for r in db.execute(
    "SELECT title FROM tasks WHERE status IN ('running','ready')").fetchall()]
```

## Worked Example (Cycle #245)

**Board state:** 19 running, 15 ready (just created and assigned), 31 free workers.

**Batch creator (running only):** "Uncovered: 0" → 0 tasks created
**Manual analysis (running + ready):** "Uncovered: 29" → 25 tasks created

The 15 ready tasks (exp_3717-3736) were invisible to the batch creator. Queue items like "Can TF-IDF+LR maintain robustness with domain shift?" appeared uncovered because no RUNNING task had that title — but exp_3747 (ready, assigned to worker-42) covered it exactly.

## Impact

Without checking ready tasks, the Director creates duplicate tasks that waste worker slots. In Cycle #245, 4 of the 25 created tasks overlapped with ready tasks (exp_3747, exp_3748, exp_3750, exp_3751).

## Prevention

1. **Always include ready tasks in coverage checks:**
   ```sql
   SELECT title FROM tasks WHERE status IN ('running', 'ready')
   ```

2. **After creating a batch of tasks, re-run coverage check** before creating more — the newly created ready tasks now provide coverage.

3. **Use the thread-level analysis pattern** from `batch-creator-coverage-false-negative.md` — it naturally includes ready tasks when checking `status IN ('running','ready')`.

4. **Sequential batch creation:** If creating tasks in multiple rounds (e.g., 15 + 7 + 3), re-check coverage between rounds. Tasks created in round 1 are ready by round 2.

## Relationship to Other Pitfalls

- **batch-creator-coverage-false-negative.md**: The batch creator's keyword overlap is too broad (marks items covered when they share common words). This pitfall is different — it's about the batch creator not seeing ready tasks at ALL.
- **batch-creator-zero-with-free-workers.md**: Related symptom (0 tasks with free workers) but different cause (keyword overlap vs. missing ready tasks).
- **manual-fallback-for-coverage-false-positives.md**: Covers the case where batch selects overlapping items. This covers the case where batch selects nothing because ready tasks provide false coverage.
