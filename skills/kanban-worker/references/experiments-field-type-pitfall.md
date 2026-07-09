# Pitfall — `experiments` field is integer, not dict

**Added:** Cycle #252 (verified recurring)

## Problem

`self_state.json`'s `experiments` field can be an integer (e.g., `2182`), not a dict with `completed`/`running` keys. Calling `.get('completed', [])` on an integer raises `AttributeError: 'int' object has no attribute 'get'`.

This breaks:
- Queue classification code (kanban-worker taxonomy section)
- `batch_create_tasks.py` (via `build_experiment_index()`)
- `curiosity_scorer.py` (via `build_experiment_index()`)
- `director.py` (via `state['experiments']['running']`)

## Fix

Use `metrics.experiments_completed_list` as the authoritative source of completed experiment IDs:

```python
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()

# Primary: metrics.experiments_completed_list (always a list)
for e in ss.get('metrics', {}).get('experiments_completed_list', []):
    if isinstance(e, str):
        m = re.search(r'exp_(\d+\w*)', e)
        if m: ss_exp_ids.add(f'exp_{m.group(1)}')
    elif isinstance(e, int):
        ss_exp_ids.add(f'exp_{e}')

# Fallback: experiments.completed (if dict)
experiments = ss.get('experiments', {})
if isinstance(experiments, dict):
    for e in experiments.get('completed', []):
        if isinstance(e, dict):
            ss_exp_ids.add(e.get('id', ''))
        elif isinstance(e, str):
            m = re.search(r'exp_(\d+\w*)', e)
            if m: ss_exp_ids.add(f'exp_{m.group(1)}')
        elif isinstance(e, int):
            ss_exp_ids.add(f'exp_{e}')
```

## Schema Variants

The `experiments` field can be:
1. **Dict**: `{"completed": [{"id": "exp_2515", ...}, ...], "running": [...]}`
2. **Integer**: `2182` (just a count)
3. **Missing**: key absent entirely

The `experiments_completed_list` under `metrics` is always a list of strings/ints, making it the reliable fallback.

## Also: bare integer entries

`experiments_completed_list` can contain bare integers (e.g., `2178`) not just `"exp_2178"`. Use `isinstance(item, int)` check when iterating.

See `references/director_string_entry_fix.md` in the director-loop skill for the full schema variants.
