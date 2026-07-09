# Pitfall: Synthesis "done" but experiments NOT in self_state.json

**Added:** 2026-06-01  
**Severity:** HIGH  
**Category:** Synthesis / self_state.json

## Description

A synthesis task can reach `status=done` with a summary claiming consolidation of experiments into self_state.json, yet the experiments are ABSENT from the `experiments.completed` array.

## Example

```
Synthesis task t_4ff21c1c summary:
"Synthesis v399 complete. 3 experiments (exp_945/950/962) consolidated 
into self_state.json (912 total, all counters reconciled)."

Verification:
- exp_945: NOT FOUND in self_state.json
- exp_950: NOT FOUND in self_state.json  
- exp_962: NOT FOUND in self_state.json
- Last 5 entries in self_state.json: exp_936, exp_965, exp_967, exp_968, exp_969
```

## Root Causes

1. **Silent write failure**: The synthesis worker's Python script failed to write to self_state.json but didn't raise an error
2. **File overwrite race**: Another process (e.g., janitor, another synthesis worker) overwrote self_state.json after the synthesis worker wrote to it
3. **Partial write**: The synthesis worker wrote some fields but not the experiments array
4. **Wrong file path**: The synthesis worker wrote to a different path than expected

## Detection

After ANY synthesis task completes, ALWAYS verify:

```python
import json, re, os

# Load self_state.json
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if eid:
        ss_exp_ids.add(eid)

# Check if synthesized experiments are present
synthesized_ids = ['exp_945', 'exp_950', 'exp_962']  # from synthesis summary
for eid in synthesized_ids:
    status = "FOUND" if eid in ss_exp_ids else "MISSING"
    print(f"{eid}: {status}")
```

## Fix

If experiments are missing after synthesis:

1. Do NOT try to re-open the completed synthesis task
2. Create a NEW synthesis task with explicit instructions to verify the write
3. Include the missing experiment IDs in the new task body
4. After the new synthesis completes, verify again

## Prevention

Include in synthesis task body:
```
CRITICAL: After writing self_state.json, VERIFY the write by reading 
the file back and checking that all experiment IDs are present.
```

**Post-completion verification (added Cycle #245):** After dispatching synthesis, check `kanban show <id> --json` for status. If status=done with 0 events and <2 min age, verify self_state.json immediately — the synthesis worker may have completed without doing the work. See "Distinct From" below for how this differs from related pitfalls.

## Distinct From

- **Comment race condition** (Cycle #188): Comment arrives after task completes. The synthesis worker never sees the comment. Fix: check event count before commenting (0 events = safe to comment).
- **Synthesis claims mismatch** (Cycle #159, #214): Synthesis summary claims experiments were consolidated, but they weren't. Fix: verify against self_state.json, not synthesis titles/summaries.
- **This pitfall**: Synthesis task completes (status=done) but self_state.json is NOT updated at all. The worker called `kanban_complete` without doing the work.

## Related Pitfalls

- "synthesis title/summary claims don't match actual coverage" (Cycle #159, #214)
- "metrics counter drift" (Cycle #152, #159, #190, #218)
