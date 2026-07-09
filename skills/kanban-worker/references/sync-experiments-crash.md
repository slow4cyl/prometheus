# sync_experiments_to_db.py — `experiments` field type variance crash

## Problem

`sync_experiments_to_db.py` crashes when `self_state.json`'s `experiments` field is an integer instead of a dict:

```
AttributeError: 'int' object has no attribute 'get'
```

The script calls `state.get('experiments', {}).get('completed', [])` — but some synthesis cycles write `experiments` as a bare integer (e.g., `2149`) rather than `{"completed": [...], "running": [...]}`.

## Same bug pattern in other scripts

This is the same `experiments` field type variance documented in the director-loop skill's pitfalls:

- `director.py` — `state['experiments']['running']` raises `TypeError`
- `curiosity_scorer.py` — `build_experiment_index()` calls `.get()` on integer
- `batch_create_tasks.py` — depends on scorer, cascading failure

## Fix pattern

```python
experiments = state.get("experiments", {})
completed = experiments.get("completed", []) if isinstance(experiments, dict) else state.get("metrics", {}).get("experiments_completed_list", [])
```

## Impact on dashboard

When the sync script crashes, the dashboard (which reads from `prometheus.db`) falls behind on new experiments. The dashboard auto-refreshes every 10s but only sees what's in the SQLite DB.

**Workaround**: Skip the sync call if it crashes. The synthesis worker's own self_state.json write should handle this field correctly. The next successful sync cycle will catch up.

## When this was observed

- June 3, 2026: Director pass attempted `sync_experiments_to_db.py` after creating synthesis task. Script crashed. `self_state.json` had `experiments: 2149` (integer).
- The synthesis worker (when it runs) should normalize this field to a proper dict with `completed` array.

## Prevention

The synthesis worker's self_state.json update code should always write `experiments` as a dict:
```python
d['experiments'] = {
    'completed': deduped_experiments,
    'running': existing_running
}
```

The sync script should also be patched to handle both formats (same pattern as the other scripts).
