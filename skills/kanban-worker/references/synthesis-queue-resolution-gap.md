# Synthesis Worker Queue Resolution Gap (added Cycle #205)

## Problem

The Director correctly identifies which queue items should be tagged `[RESOLVED by exp_XXX]` and communicates this to the synthesis worker via task body or comments — but the synthesis worker consistently fails to actually tag them.

Result: queue accumulates 30+ items all classified as DONE (source experiment completed) but zero tagged RESOLVED. The queue grows monotonically because items are never cleaned up.

## Detection

During Director queue triage, if ALL items are DONE but zero are RESOLVED, the synthesis workers are not performing resolution.

```python
# Quick check: are any items RESOLVED?
resolved = sum(1 for item in queue if 'RESOLVED' in item.get('text', str(item)))
print(f"Resolved: {resolved}/{len(queue)}")
# If resolved == 0 and queue > 20, synthesis workers are skipping resolution
```

## Root Cause

The synthesis worker's primary objective is updating self_state.json (experiments, counters, knowledge graph). Queue item resolution is a secondary task that gets skipped when the synthesis worker is focused on the primary objective.

## Fix Options

1. **Add explicit queue resolution instructions to synthesis task body:**
   ```
   After updating self_state.json, scan curiosity_queue and tag items as
   [RESOLVED by exp_XXX] if the experiment answered the question.
   ```

2. **Create dedicated curation task** when backlog exceeds 20 unresolved items.

3. **Include queue resolution as mandatory synthesis step** (not optional).

## Prevention

The synthesis task body should list specific queue items to resolve, not just experiments to synthesize:

```
Also resolve queue items 3, 8, 15 — their source experiments (exp_570,
exp_562, exp_617) are now completed and their questions are answered.
```

## Observed in Cycle #205

- Queue had 32 items, all classified DONE, zero RESOLVED
- 3 unsynthesized experiments found (exp_650, exp_658, exp_668)
- Synthesis task created and dispatched
- Queue items still unresolved after synthesis
