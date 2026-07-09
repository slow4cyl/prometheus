# Synthesis Counter Reconciliation — Anti-Drift Pattern

## The Problem

Metrics drift is the #1 persistent issue in the Director loop. Multiple synthesis cycles append experiment IDs to the `experiments.completed` array without updating the derived counter fields (`experiments_completed`, `experiments_conducted`, `experiments_completed_count`). The drift accumulates over time:
- Cycle #152: 3-entry drift
- Cycle #159: 3-entry drift  
- Cycle #190: 163-entry drift (catastrophic escalation)
- Cycle #218: 35-entry drift (751 in array, 716 in counters)

## Root Cause

Synthesis workers append to `experiments.completed` (the array) but don't update the counter fields in `metrics`. Each synthesis cycle adds ~3 experiments, so the drift grows by ~3 per cycle.

## The Fix (Atomic Reconciliation)

After ANY write to `experiments.completed`, set ALL counter fields to the array length in a SINGLE atomic write:

```python
import json, os
from datetime import datetime, timezone

path = os.path.expanduser('~/.hermes/self_state.json')
d = json.load(open(path))

# Deduplicate experiments.completed by ID
seen_ids = set()
deduped = []
for exp in d.get('experiments', {}).get('completed', []):
    eid = exp.get('id', '') if isinstance(exp, dict) else ''
    if eid and eid not in seen_ids:
        seen_ids.add(eid)
        deduped.append(exp)
d['experiments']['completed'] = deduped

# Compute actual count from the authoritative array
actual_count = len(d['experiments']['completed'])

# Set ALL counters to the same value — NO exceptions
d['metrics']['experiments_completed'] = actual_count
d['metrics']['experiments_conducted'] = actual_count
d['metrics']['experiments_completed_count'] = actual_count

# Deduplicate the ID list too
completed_ids = list(dict.fromkeys(
    e.get('id', '') if isinstance(e, dict) else str(e)
    for e in d['experiments']['completed']
))
d['metrics']['experiments_completed_list'] = completed_ids

# Update metadata
d['version'] = d.get('version', 0) + 1
d['last_updated'] = datetime.now(timezone.utc).isoformat()

with open(path, 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)

# Verification step — ALWAYS run this after writing
verify = json.load(open(path))
actual = len(verify['experiments']['completed'])
for field in ['experiments_completed', 'experiments_conducted', 'experiments_completed_count']:
    assert verify['metrics'][field] == actual, f"DRIFT: {field}={verify['metrics'][field]} != actual={actual}"
print(f"Counter reconciliation verified: {actual} experiments, all counters match")
```

## Synthesis Task Body Template (with Counter Enforcement)

When creating synthesis tasks as Director, include CONCRETE counter values:

```
SYNTHESIS CYCLE: Read all completed Kanban tasks since last synthesis.
Experiments to synthesize: exp_XXX (one-line summary), exp_YYY (one-line summary).

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \
  --task-id $HERMES_KANBAN_TASK \
  --experiments "exp_NNN,..." \
  --resolutions '[{"item": N, "resolved_by": "exp_XXX", "note": "..."}]' \
  --curiosities "Follow-up 1?;Follow-up 2?"
This writes to the synthesis_outputs table. synthesis_merger.py (cron 2m) applies to self_state.json.

CRITICAL COUNTER RECONCILIATION:
Current state: {actual_count} experiments in array, counters say {counter_value} (drift of {drift})
After update: ALL counter fields must equal len(experiments.completed array)
DO NOT just append to the array without updating counters.
Verify before writing: if any counter != actual_count, the write is WRONG.
Preserve phase number (never decrease it).
```

## Detection

After each synthesis cycle, verify counters match:

```python
d = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
actual = len(d['experiments']['completed'])
for field in ['experiments_completed', 'experiments_conducted', 'experiments_completed_count']:
    if d['metrics'][field] != actual:
        print(f"DRIFT: {field}={d['metrics'][field]} != actual={actual}")
```

## `last_synthesis` Timestamp Drift (added 2026-06-02)

The `last_synthesis` field in self_state.json can become stale relative to the actual latest synthesis. The synthesis worker updates `audit_trail` entries (with accurate timestamps) but may not update `last_synthesis`. In one observation, `last_synthesis` showed `06:01:14` while `audit_trail[-1].timestamp` showed `06:26:03` — a 25-minute gap.

**Impact:** The pre-run skip threshold uses `self_state.json`'s `last_updated` timestamp (not `last_synthesis`), but if the Director's assessment logic ever uses `last_synthesis` to determine recency, it would be based on stale data.

**Fix:** Synthesis workers should update `last_synthesis` to `datetime.now(timezone.utc).isoformat()` in the same atomic write that updates counters and the experiments array. Add to the reconciliation code:

```python
d['last_synthesis'] = datetime.now(timezone.utc).isoformat()
```

**Detection:** Compare `last_synthesis` against `audit_trail[-1].timestamp`. If they differ by more than a few minutes, the field is stale.

## `experiments_completed_list` Type Corruption (added 2026-06-02)

A synthesis worker can corrupt `experiments_completed_list` from a list to an integer by writing the length instead of the list itself:

```python
# WRONG — writes int instead of list
d['metrics']['experiments_completed_list'] = len(completed_ids)

# RIGHT — writes the actual list
d['metrics']['experiments_completed_list'] = completed_ids
```

**Observed:** Field stored as `1524` (int) instead of `["exp_679", "exp_681", ...]` (list). This breaks any code that iterates over the field (`TypeError: 'int' object is not iterable`).

**Detection:** Check `type(d['metrics']['experiments_completed_list'])`. If it's `int`, the field is corrupted.

**Fix:** In the reconciliation code, always assign the list, not the length:
```python
d['metrics']['experiments_completed_list'] = completed_ids  # NOT len(completed_ids)
```

**Prevention:** Add a type assertion in the verification step:
```python
assert isinstance(verify['metrics']['experiments_completed_list'], list), \
    f"experiments_completed_list is {type(verify['metrics']['experiments_completed_list'])}, expected list"
```

## Why This Keeps Happening

1. Synthesis workers focus on adding new experiments (the interesting part) and treat counter updates as boilerplate
2. The array append and counter update are in different code blocks, so workers sometimes skip the counter block
3. There's no runtime assertion — the drift is silent until someone checks
4. `last_synthesis` is treated as metadata rather than a functional field — workers update it casually or not at all
5. Workers confuse "update the counter" with "update the list" — writing `len()` instead of the list itself (type corruption, June 2026)

The fix is making the counter reconciliation a SINGLE atomic operation with a verification step, not a separate code block.
