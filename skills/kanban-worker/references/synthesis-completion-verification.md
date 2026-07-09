# Synthesis Completion Verification

Added Cycle #201 — documents the pattern where a synthesis task completes but doesn't fully update self_state.json.

## The Problem

A synthesis task can reach `status=done` with a summary claiming "consolidated N experiments" — but self_state.json only contains a subset of those experiments. The summary reflects the worker's *intent*, not the actual file I/O result.

**Observed in Cycle #201:** t_9a74c6ba claimed 6 experiments consolidated (exp_528, exp_539, exp_571, exp_573, exp_599, exp_600) but only 3 were actually written to self_state.json (exp_528, exp_539, exp_571). The synthesis worker called `kanban_complete` before verifying its write.

## Root Causes

1. Token limits — the worker runs out of output budget during the self_state.json write phase
2. API errors during file write — the write silently fails
3. Early termination — the worker hits a timeout or error before completing the write
4. The worker trusts its own "I wrote it" without reading back to verify

## Detection

After ANY synthesis task completes, verify by checking which experiment IDs from the synthesis body are actually present in `self_state.json`'s `experiments.completed` array:

```python
import json, re, os

# Load the synthesis task body to get expected experiment IDs
synth_body = "..."  # from kanban_show
expected_ids = set(f'exp_{m.group(1)}' for m in re.finditer(r'exp_(\d+\w*)', synth_body))

# Load self_state.json
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
actual_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if eid: actual_ids.add(eid)

missing = expected_ids - actual_ids
if missing:
    print(f"INCOMPLETE SYNTHESIS: {len(missing)} experiments missing: {missing}")
```

Do NOT trust the summary text alone — always verify against self_state.json.

## Fix

Create a follow-up synthesis task for the missing experiments:

```bash
hermes kanban create "SYNTHESIS: consolidate N missing experiments (exp_XXX, ...)" \
  --assignee prometheus-synthesis \
  --body "SYNTHESIS CYCLE: Read self_state.json first.

The previous synthesis task (TASK_ID) claimed to consolidate N experiments but only added M to self_state.json. Missing experiments:
- exp_XXX: <one-line summary>
- exp_YYY: <one-line summary>

CRITICAL: Read self_state.json FIRST. Write via write_synthesis_output.py (NOT to self_state.json directly). synthesis_merger.py (cron 2m) applies to self_state.json."
```

## Prevention

The synthesis worker should verify its own write by reading back self_state.json after writing and confirming all expected experiments are present before calling `kanban_complete`. Add a verification step to the synthesis workflow:

```python
# After writing self_state.json
ss_after = json.load(open(ss_path))
actual_after = {e.get('id','') for e in ss_after.get('experiments',{}).get('completed',[]) if isinstance(e, dict)}
if not expected_ids.issubset(actual_after):
    missing = expected_ids - actual_after
    raise RuntimeError(f"Write verification failed: {len(missing)} experiments missing: {missing}")
```

## Related Pitfalls

- "synthesis title claims don't match actual coverage" — about trusting titles vs actual data
- "Metrics counter drift" — about array length vs metrics counters diverging
- "comment race condition" — about comments arriving after synthesis completes
