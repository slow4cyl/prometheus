# Unsynchronized Re-check After Dispatch (Cycle #270)

## Problem

When the Director creates a synthesis task and dispatches it, the unsynthesized experiment count can change during the same pass because new done tasks appear between checks. Workers complete while the Director is running, adding to the done list mid-pass.

## Example

In Cycle #270:
1. First unsynthesized check found 3 experiments: exp_935, exp_938, exp_939
2. Created synthesis task `t_b45cccb5` and dispatched it
3. Re-check after dispatch found 5 experiments: exp_935, exp_937, exp_938, exp_939, exp_944
4. exp_937 and exp_944 had completed between the two checks

## Detection

After creating and dispatching a synthesis task, re-run unsynthesized detection:

```python
import json, re, os

# Re-dump board state (tasks may have completed since last dump)
# hermes kanban list --status done --json > /tmp/kanban_done.json

done = json.load(open('/tmp/kanban_done.json'))
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))

done_exp_ids = set()
for t in done:
    title = t.get('title', '')
    is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
    if not is_synth:
        for m in re.finditer(r'exp_(\d+\w*)', title):
            done_exp_ids.add('exp_' + m.group(1))

ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if eid: ss_exp_ids.add(eid)

unsynth = done_exp_ids - ss_exp_ids
# Compare against the set you already sent to synthesis
already_queued = {'exp_935', 'exp_938', 'exp_939'}  # from first check
new_unsynth = unsynth - already_queued
if new_unsynth:
    print(f"NEW unsynthesized since dispatch: {new_unsynth}")
    # Add via kanban_comment to the existing synthesis task
```

## Fix

Add newly-discovered experiments to the existing synthesis task via `kanban_comment`:

```bash
hermes kanban comment <synthesis_task_id> "ADDITIONAL: Also synthesize exp_937 and exp_944. These were discovered after initial task creation and are NOT covered by any other synthesis task."
```

Do NOT create a second synthesis task — this risks concurrent writes to self_state.json.

## Prevention

Build the re-check into your Director workflow:
1. Detect unsynthesized → create synthesis task → dispatch
2. Re-dump done list → re-detect unsynthesized → add any new ones via comment
3. Log both the initial and updated counts in the audit trail

## Related

- "Late-completing experiments (added Cycle #147)" — same fix pattern (kanban_comment), different trigger
- "Comment race condition — synthesis completes before comment arrives (added Cycle #188)" — if synthesis finishes before comment arrives, create a new synthesis task instead
