# Synthesis Dual-Format Experiment Entry Pitfall (added Cycle #214)

## Problem

The synthesis worker may add entries to `self_state.json`'s `experiments.completed` array in the OLD format (`keys=['name', 'result']`) instead of the new format (`keys=['id', 'hypothesis', 'result', ...]`). Code that only checks `e.get('id', '')` misses these entries, producing false "unsynthesized" counts.

## Impact

In Cycle #214, the Director's unsynthesized detection found 16 "unsynthesized" experiments when only 2 were genuinely unsynthesized. The 14 false positives were entries added by synthesis v346 in the old format. This wasted diagnostic time and could lead to unnecessary duplicate synthesis tasks.

## Detection

Check the last 20 entries in `self_state.json`'s `experiments.completed` array:

```python
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
experiments = ss.get('experiments', {}).get('completed', [])
for e in experiments[-20:]:
    if isinstance(e, dict):
        keys = list(e.keys())
        fmt = 'NEW' if 'id' in keys else 'OLD'
        print(f"  {e.get('id', e.get('name', '?'))}: format={fmt}, keys={keys[:5]}")
```

If any entries show `format=OLD`, the dual-format issue is active.

## Fix

Always check BOTH `e.get('id', '')` and `e.get('name', '')` when extracting experiment IDs:

```python
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    if isinstance(e, dict):
        eid = e.get('id', '')
        if not eid:
            eid = e.get('name', '')  # OLD format fallback
        if eid and not eid.startswith('exp_'):
            m2 = re.search(r'exp_(\d+\w*)', eid)
            if m2: eid = f'exp_{m2.group(1)}'
    elif isinstance(e, str):
        m2 = re.search(r'exp_(\d+\w*)', e)
        eid = f'exp_{m2.group(1)}' if m2 else ''
    else:
        eid = ''
    if eid: ss_exp_ids.add(eid)
```

## Prevention

The synthesis worker should consistently use the NEW format (`keys=['id', 'hypothesis', 'result', ...]`) when adding entries. However, since the synthesis worker is a separate process that may be running old code, the Director's detection code must handle both formats.

## Related Pitfalls

- "Metrics counter drift" — synthesis worker updates array but not counters
- "Synthesis title/summary claims don't match actual coverage" — trust self_state.json, not titles
