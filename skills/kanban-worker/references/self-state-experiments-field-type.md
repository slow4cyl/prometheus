# self_state.json Experiments Field Type Variance (added Cycle #245)

The `experiments` field in self_state.json can be an **integer** (e.g., `2178`), not a dict with `completed`/`running` keys. This breaks code that assumes the dict format.

## Schema Variants

**Variant A (int)** — current production format:
```json
{
  "experiments": 2178,
  "metrics": {
    "experiments_completed_list": ["exp_679", "exp_681", ...],
    "experiments_completed": 2178
  }
}
```

**Variant B (dict)** — historical format:
```json
{
  "experiments": {
    "completed": [{"id": "exp_679", ...}, ...],
    "running": [...]
  }
}
```

## Safe Reading Pattern

```python
import json, re, os

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))

# Safe: handles both variants
experiments = ss.get('experiments', {})
if isinstance(experiments, dict):
    completed = experiments.get('completed', [])
    ss_exp_ids = set()
    for e in completed:
        if isinstance(e, dict):
            ss_exp_ids.add(e.get('id', ''))
        elif isinstance(e, str):
            m = re.search(r'exp_(\d+)', e)
            if m:
                ss_exp_ids.add(f'exp_{m.group(1)}')
else:
    # experiments is an int — completed list is in metrics
    completed_list = ss.get('metrics', {}).get('experiments_completed_list', [])
    ss_exp_ids = set(completed_list)

# Alternative: always read from metrics (works in both variants)
ss_exp_ids = set(ss.get('metrics', {}).get('experiments_completed_list', []))
```

## Why This Matters

The Director synthesis code in the main SKILL.md uses:
```python
ss.get('experiments',{}).get('completed',[])
```
This fails with `AttributeError: 'int' object has no attribute 'get'` on Variant A.

**Fix**: Always read from `metrics.experiments_completed_list` instead. It exists in both variants and is the authoritative source.

## Detection

If you see `AttributeError: 'int' object has no attribute 'get'` when reading self_state.json, the `experiments` field is an int. Switch to the `metrics` path.

## Related

This same issue affects `director.py`, `curiosity_scorer.py`, and `batch_create_tasks.py`. See `director-loop` skill pitfalls for the full list of affected scripts.
