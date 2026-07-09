# Batch Creator Queue Starvation — Low-Score Threshold Effect

**Added:** Cycle #578 (June 2, 2026)
**Related:** `batch-create-conservative-pitfall.md` (conservative task count)
**Category:** Director workflow, queue management

## Problem

The `batch_create_tasks.py --min-score 30` threshold filters out queue items
before counting them as "active." When most queue items score below 30, the
script reports few active items and creates fewer tasks than free workers
can handle — even when the queue has 50+ items.

**Observed in Cycle #578:**
- Queue: 57 NO_SOURCE items (all genuinely uncovered)
- Script output: "Queue: 13 active items, 13 score >= 30"
- Result: 7 tasks created for 9 free workers (2 workers idle)
- 44 items (77% of queue) were never considered

## Root Cause

The scoring function penalizes items that:
1. Lack experiment source IDs (NO_SOURCE items always score lower)
2. Reference narrow subtopics (e.g., "FPGA implementation" scores lower than
   "injection detection generalization")
3. Were added by synthesis workers (vague framing lowers keyword overlap)

But these items may still be valuable — they represent genuine knowledge gaps
that no running experiment addresses.

## Detection

When `batch_create_tasks.py` reports:
- "Uncovered by running tasks: N" where N < free_workers
- OR "Selected: M tasks" where M < 50% of free workers

...and the queue has 20+ items, the threshold is starving the queue.

## Mitigation

**Option A: Lower the threshold**
```bash
python3 ~/.hermes/scripts/batch_create_tasks.py --count 9 --min-score 15
```
This captures more items but risks low-value tasks.

**Option B: Manual creation for remainder**
After the script creates its batch, manually create tasks for 2-3 high-value
NO_SOURCE items that scored below the threshold. Use the queue classification
to identify the best candidates.

**Option C: Two-pass approach**
1. Run script with default threshold for high-confidence items
2. Run again with lower threshold (--min-score 10) for remaining workers
3. Deduplicate by title similarity

## When to Apply

- Free workers ≥ 3 AND queue has 20+ items
- Script creates < 50% of free worker slots
- Queue items are all NO_SOURCE (no experiment references to score on)

## Prevention

The Director prompt should include: "If batch_create_tasks.py creates fewer
tasks than 50% of free workers, supplement with manual creation for uncovered
NO_SOURCE queue items."
