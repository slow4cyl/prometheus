# Director — Unsynthesized Count Inconsistency (Cycle #245)

## Problem

The Director's unsynthesized detection (Method A: `done_exp_ids - ss_exp_ids`) produced **different counts on consecutive checks within the same pass** — 25 on the first check, 8 on the second. This caused confusion about whether synthesis was needed.

## Root Cause

Two issues:

### 1. Incomplete self_state.json parsing

The first check used `re.search(r'(\d+)', item)` on `experiments_completed_list` entries. When entries are bare integers (e.g., `2640` instead of `"exp_2640"`), this regex matches the integer correctly. But when entries are strings like `"exp_2640"`, it matches the first number in the string. The second check added explicit `isinstance(item, int)` handling and counted MORE entries (2417 vs 2400), reducing the gap.

### 2. Different regex patterns on done titles

The first check used `\bexp_(\d+)\b` with `re.search()` (finds first match per title). The second check used `re.finditer()` (finds ALL matches per title). Titles like "SYNTHESIS: consolidate exp_2640, exp_2669" contain multiple experiment IDs — `finditer` captures all of them while `search` only captures the first.

## Correct Pattern

Use a single, consistent approach for both done_exp_ids and ss_exp_ids:

```python
import json, re, os

# Done exp IDs — use finditer to capture ALL IDs per title
done_exp_ids = set()
for t in done:
    for m in re.finditer(r'\bexp_(\d+)\b', t.get('title', '')):
        done_exp_ids.add(int(m.group(1)))

# self_state completed — handle ALL entry formats
ss_completed = set()
state = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
exps = state.get('experiments_completed_list',
         state.get('metrics', {}).get('experiments_completed_list', []))
for item in exps:
    if isinstance(item, int):
        ss_completed.add(item)
    elif isinstance(item, str):
        m = re.search(r'(\d+)', item)
        if m: ss_completed.add(int(m.group(1)))
    elif isinstance(item, dict):
        eid = item.get('id', item.get('experiment_id', ''))
        m = re.search(r'(\d+)', str(eid))
        if m: ss_completed.add(int(m.group(1)))

unsynth = done_exp_ids - ss_completed
```

**Key rules:**
- Always use `re.finditer()` (not `re.search()`) on done titles to capture all experiment IDs
- Always check `isinstance(item, int)` first for self_state entries
- Check BOTH `experiments_completed_list` (top-level) AND `metrics.experiments_completed_list` (nested)
- Use the SAME regex and parsing logic for both sets — inconsistency causes phantom unsynthesized entries

## Impact

With the wrong pattern, the Director may:
- Create unnecessary synthesis tasks (phantom unsynthesized count > actual)
- Skip necessary synthesis (if the count is accidentally low)
- Waste a synthesis worker cycle on experiments already consolidated
