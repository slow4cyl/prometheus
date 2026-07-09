# batch_create_tasks.py Bug Fixes

## UnboundLocalError in --json output (fixed Cycle #250)

### Problem
When running `batch_create_tasks.py --json`, the script crashes with:
```
UnboundLocalError: cannot access local variable 'i' where it is not associated with a value
```

### Root Cause
Line 304 used `i` in a list comprehension but `i` was not defined in that scope:
```python
"tasks": [{
    "title": f"exp_{next_id[0] + i}: {s['text'][:80]}",
    ...
} for s in selected]
```

### Fix
Changed to use `enumerate()`:
```python
"tasks": [{
    "title": f"exp_{next_id[0] + idx}: {s['text'][:80]}",
    ...
} for idx, s in enumerate(selected)]
```

### Location
`~/.hermes/scripts/batch_create_tasks.py`, line 304

## Conservative Task Creation

The script may create fewer tasks than expected due to:
1. **Thread diversity cap**: 35% max per thread (e.g., with 6 free workers, max 2 per thread)
2. **Score threshold**: Only items with `score >= min-score` (default: 30) are considered
3. **Uncovered filter**: Only items not covered by running tasks are selected

### Workaround
If the script creates fewer tasks than free workers:
1. Check `--dry-run --json` output to see what was selected
2. Lower `--min-score` to include more items
3. Or create tasks manually for remaining workers

## Related

- `cross-platform-workspace-path-mismatch.md` - workspace path issues
