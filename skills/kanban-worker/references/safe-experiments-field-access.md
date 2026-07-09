# Safe self_state.json Experiments Field Access

## Problem
`self_state.json` has two schema variants for the `experiments` field:
1. **Dict variant** (older): `{"completed": [...], "running": [...]}`
2. **Int variant** (current): just an integer count (e.g., `2171`)

Code that assumes the dict variant crashes with `AttributeError: 'int' object has no attribute 'get'` or `TypeError: 'int' object is not subscriptable`.

## Safe Access Pattern

```python
import json, re, os

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))

# SAFE: Always use experiments_completed_list from metrics
ss_exp_ids = set()
completed_list = ss.get('metrics', {}).get('experiments_completed_list', [])
for e in completed_list:
    if isinstance(e, str):
        m = re.search(r'exp_(\d+)', e)
        if m: ss_exp_ids.add(f'exp_{m.group(1)}')
    elif isinstance(e, dict):
        eid = e.get('id', '')
        if eid:
            m2 = re.search(r'exp_(\d+)', eid)
            if m2: ss_exp_ids.add(f'exp_{m2.group(1)}')
    elif isinstance(e, int):
        # Bare integer entries are valid experiment IDs (e.g., 2178 means exp_2178)
        ss_exp_ids.add(f'exp_{e}')
# DO NOT skip int entries — they are experiment IDs, not counts

# UNSAFE — crashes when experiments is an int:
# completed = ss.get('experiments', {}).get('completed', [])
```

## Detection
If you see `AttributeError: 'int' object has no attribute 'get'` on `ss.get('experiments', {})`, the field is an int. Switch to `metrics.experiments_completed_list`.

## Context
- `metrics.experiments_completed_list` is the authoritative source (same data, always a list)
- `metrics.experiments_completed` is a derived int count
- The `experiments` top-level field can be either a dict or an int depending on which synthesis worker last wrote self_state.json
- Both paths contain the same experiment IDs — `experiments_completed_list` is just the safe way to access them
