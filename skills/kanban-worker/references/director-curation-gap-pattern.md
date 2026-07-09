# Director Curation Gap — Queue Accumulation of Untagged DONE Items

**Added:** Cycle #212
**Category:** Director maintenance gap

## Problem

When the Director focuses only on synthesis (unsynthesized experiments) and ignores curation (queue item tagging), the queue accumulates untagged DONE items. In Cycle #212, the queue had 53 items total, but 44 were DONE (source experiment completed but not tagged RESOLVED). This creates a misleading queue state where:
- The queue appears full of active questions
- Many items are actually answered but not tagged
- Future Director passes may create redundant tasks for already-answered questions

## Example

```
Queue classification (Cycle #212):
  RESOLVED: 7     ← properly tagged
  RUNNING: 0      ← no items depend on running experiments
  DONE: 44        ← source experiments completed, not tagged
  NO_SOURCE: 2    ← pure research questions
  UNKNOWN: 0

Running tasks: 17 (high saturation)
```

The Director created a synthesis task for exp_730-733 (unsynthesized experiments) but did NOT create a curation task for the 44 DONE items.

## Root Cause

The Director prioritized synthesis over curation. The existing guidance says:
- "SYNTHESIS AND CURATION TASKS ARE MAINTENANCE, NOT INVESTIGATION"
- "Queue >50 items: run curation as the investigation task"

But the Director interpreted this as "choose one maintenance task" rather than "create both when both are needed."

## Fix

When both conditions are met in a Director pass:
1. 3+ unsynthesized experiments → CREATE SYNTHESIS TASK
2. Queue >50 items with >30 DONE/untagged → CREATE CURATION TASK

Both tasks are independent and can run in parallel. The synthesis task updates self_state.json with experiment results. The curation task tags resolved items and removes duplicates.

## Prevention

The Director pass should check BOTH conditions:
- Check for unsynthesized experiments (done tasks vs self_state.json)
- Check queue classification (RESOLVED/RUNNING/DONE/NO_SOURCE counts)

If both maintenance needs exist, create both tasks in the same pass.

## Context

In Cycle #212:
- 3 hung tasks reclaimed (exp_695, exp_700, exp_701) — zero CPU after 45+ min
- 1 synthesis task created for exp_730-733
- 0 curation tasks created (missed opportunity)
- Queue ended with 44 untagged DONE items

The synthesis worker will tag some resolved items during its synthesis pass, but a dedicated curation task is more reliable for comprehensive tagging.
