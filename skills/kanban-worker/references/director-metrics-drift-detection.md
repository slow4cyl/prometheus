# Director Metrics Drift Detection

Added after Cycle #248. The Director should flag metrics counter drift when detected, even though only the synthesis worker fixes it.

## Pattern

After loading self_state.json, compare:
- `len(experiments.completed)` array
- `metrics.experiments_completed` counter
- `metrics.experiments_completed_list` length

If any differ, note the drift in the audit log and include concrete values in the synthesis task body so the synthesis worker has a verification target.

## Example (from Cycle #248)

```
experiments.completed array: 1159
metrics.experiments_completed: 1151
metrics.experiments_completed_list length: 868
→ 8-entry drift (array vs metrics counter)
→ 291-entry drift (array vs list length)
```

## Why this matters

The synthesis worker needs to know the CURRENT drift values to verify reconciliation. If the Director just says "reconcile counters", the synthesis worker may set them to the wrong value. Concrete numbers prevent this.

## What to include in synthesis task body

```
CRITICAL: Reconcile metrics drift:
- experiments.completed array length: {actual_array_length}
- metrics.experiments_completed: {current_counter_value}
- Set ALL counters to {actual_array_length}: experiments_completed, experiments_conducted, experiments_completed_count
```
