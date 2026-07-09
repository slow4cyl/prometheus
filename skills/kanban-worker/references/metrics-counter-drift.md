# Metrics Counter Drift — Complete History

## Summary

The `experiments.completed` array length can diverge from the metrics counters (`experiments_completed`, `experiments_conducted`, `experiments_completed_count`) when synthesis workers update the array but don't fully reconcile the counters.

## Observed Drift Progression

| Cycle | Array Length | Metrics Counter | Drift | Direction |
|-------|-------------|----------------|-------|-----------|
| #152  | 200         | 197            | +3    | Array > Metrics |
| #159  | 270         | 267            | +3    | Array > Metrics |
| #190  | 480         | 317            | +163  | Array > Metrics |
| #201  | 552         | 683            | -131  | **Metrics > Array** |

## Key Insight: Bidirectional Drift

Drift can go in **both directions**:
- **Positive drift** (Array > Metrics): Synthesis appends to array but doesn't update counters. Previous assumption was drift is always positive.
- **Negative drift** (Metrics > Array): Counters were incremented without corresponding array entries. Observed in Cycle #201 with -131 drift.

## Prevention

Every synthesis worker must:
1. Use the `experiments.completed` array length as the **authoritative count**
2. Set ALL counter fields to `len(completed)` in a single atomic write
3. Do NOT just append to the array — always reconcile counters

## Diagnostic

When reading self_state.json, compare:
```python
arr_len = len(ss.get('experiments', {}).get('completed', []))
mc = ss.get('metrics', {}).get('experiments_completed', 0)
if arr_len != mc:
    print(f"DRIFT: array={arr_len} vs metrics={mc} (diff={arr_len - mc})")
```

## Director Action

When drift detected:
- Create a dedicated SYNTHESIS task for counter reconciliation
- Task body should state current drift values
- Instruct synthesis worker to set all counters to `len(completed)`
