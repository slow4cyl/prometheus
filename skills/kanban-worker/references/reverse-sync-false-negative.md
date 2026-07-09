# Reverse Sync Script False Negative (Cycle #600)

## Problem

`sync_sqlite_to_state.py` reports "No changes needed" even when experiments ARE missing from `self_state.json`. The script's change-detection logic doesn't catch all divergence cases.

## Example

```
$ python3 ~/.hermes/scripts/sync_sqlite_to_state.py
Sync SQLite -> self_state.json
  No changes needed

$ python3.11 -c "import json,os; ss=json.load(open(os.path.expanduser('~/.hermes/self_state.json'))); ..."
Total completed: 2052
  exp_2160: MISSING
  exp_2168: MISSING
  exp_2177: MISSING
  exp_2178: MISSING
```

Synthesis task claimed consolidation of exp_2160, exp_2168, exp_2177 but they never made it into self_state.json.

## Root Cause

The reverse sync script likely compares version numbers or timestamps rather than doing a full set-difference on experiment IDs. When the synthesis worker's writes were blocked by the approval system, the version number may have incremented (from the partial write attempt) without the experiments actually being added to the completed array.

## Detection

Always verify AFTER running reverse sync:

```python
import json, os
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
completed = ss.get('experiments', {}).get('completed', [])
print(f'Total: {len(completed)}')
# Check specific IDs from synthesis summary
for eid in ['exp_NNN', 'exp_MMM']:
    found = any((e.get('id','') if isinstance(e, dict) else '') == eid for e in completed)
    print(f'  {eid}: {"FOUND" if found else "MISSING"}')
```

## Fix

If reverse sync reports "No changes needed" but experiments are missing:

1. The synthesis worker's writes were blocked at the application level
2. Create a NEW synthesis task with explicit instructions to verify the write
3. Include the missing experiment IDs in the new task body
4. After completion, verify again

Do NOT rely on the reverse sync script as the sole fix for approval-blocked synthesis. It's a workaround that works in some cases but not all.

## Related

- "Approval system blocks synthesis worker writes" (June 2026) in kanban-worker SKILL.md
- "synthesis done but not written" reference file
- "Synthesis Coverage Verification" in director-loop SKILL.md
