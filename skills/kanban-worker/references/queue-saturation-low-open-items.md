# Queue Saturation with Low Open Items (Cycle #245)

## Problem

The queue can have many items (40+) but very few genuinely open ones (2-3). When the saturation exception check runs (`free_workers ≥ 3 AND 3+ genuinely OPEN queue items`), the open-items threshold fails even with abundant free workers.

This leaves workers idle despite available work slots.

## Root Cause

Queue items accumulate faster than curation tags them RESOLVED. Most items are either:
- Tagged RESOLVED (but curation lags behind experiment completions)
- Covered by running experiments (RUNNING category)
- Source experiment completed but question not fully answered (DONE category)

The RESOLVED tagging lags behind experiment completions because synthesis workers don't always tag items during Step 2.

## Detection

After classifying queue items, count genuinely open items:
- NO_SOURCE items with low coverage (<0.15)
- DONE/ACTIVE items whose question is NOT fully answered

If count < 3 but free_workers ≥ 3, the queue needs curation, not new tasks.

## Example (Cycle #245)

```
Queue: 43 items
Genuinely open: 2 (items 17 and 18)
Free workers: 11
Saturation exception: NOT met (2 < 3 open items)
Result: 11 workers idle despite 43 queue items
```

## Fix

Create a curation task as the investigation task for that cycle:
- Tag resolved items
- Remove duplicates
- Keep queue at 30-50 items
- Surface genuinely open items for the next Director pass

## Prevention

The synthesis worker should tag resolved items during Step 2 (self_state update) BEFORE curation reads the queue. Without this, curation cannot distinguish resolved from unresolved items.

## Diagnostic: done_exp_ids < ss_exp_ids

When self_state.json has MORE experiment IDs than the kanban done list, it means experiments were synthesized (added to self_state) without their kanban tasks being in the done list. This can happen if:
- Experiments were added manually to self_state.json
- The kanban done list is stale (tasks archived or cleaned up)
- Synthesis workers added experiments that were never kanban tasks

This is a useful diagnostic signal for data sync issues. In Cycle #245:
- done_exp_ids: 1483
- ss_exp_ids: 1556
- Delta: 73 experiments in self_state without kanban done tasks

This delta should be monitored. If it grows over time, it indicates synthesis is outpacing kanban task completion.
