# Wrong Field for Unsensed Detection (2026-06-03)

## The Bug

The unsynthesis detection code (Method A) in the Director Synthesis workflow uses `ss.get('experiments_completed_list', [])` at the top level of self_state.json. This returns only ~23 items. The actual synthesis data lives in `metrics.experiments_completed_list` (2118+ items). Using the wrong field inflates the unsynthesized count from 31 to 1693.

## Root Cause

self_state.json has multiple `experiments_completed_list` fields:
- **Top-level** `experiments_completed_list`: Only 23 items (stale, rarely updated by synthesis workers)
- **`metrics.experiments_completed_list`**: 2118 items (the field synthesis workers actually write to)
- **Top-level `experiments`**: Integer count (2149), not a dict with `running`/`completed` keys

## Impact

Director reports 1693 unsynthesized experiments instead of the actual 31. Creates unnecessary synthesis tasks and wastes worker capacity.

## Fix

In the unsynthesis detection code (Method A), replace:
```python
ss_exp_ids = set()
for e in ss.get('experiments_completed_list', []):
    m = re.search(r'exp_(\d+)', str(e))
    if m: ss_exp_ids.add(f'exp_{m.group(1)}')
```

With:
```python
ss_exp_ids = set()
for e in ss.get('metrics', {}).get('experiments_completed_list', []):
    m = re.search(r'exp_(\d+)', str(e))
    if m: ss_exp_ids.add(f'exp_{m.group(1)}')
```

## Detection

If unsynthesized count is 1000+ but running tasks only show ~16 experiments, suspect wrong field usage. Cross-check: `len(metrics.experiments_completed_list)` should be close to `len(done_exp_ids)`.

## Related

- `director.py` also needs the same fix — it accesses `state['experiments']` as a dict but it is an integer
- See `director-loop` skill `references/unsynth-detection-regex-false-positive.md` for the regex false positive variant
