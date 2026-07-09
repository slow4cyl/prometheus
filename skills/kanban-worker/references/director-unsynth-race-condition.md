# Unsynthesized Count Race Condition (Cycle #197)

## Problem

Between the Director's first unsynthesized check (finding N experiments) and the second check (after creating the synthesis task), `self_state.json` can be updated by the synthesis task or another process.

## Observed in Cycle #197

1. **First check**: Found 8 unsynthesized experiments (exp_565, 566, 567, 568, 579, 580, 582, 584)
2. **Created synthesis task**: t_d96979fb "SYNTHESIS: consolidate 8 unsynthesized experiments"
3. **Second check**: Found 0 unsynthesized experiments — `self_state.json` had been updated
4. **Queue also changed**: 47 → 54 items (7 more resolved)

## Root Cause

The synthesis task or another process updated `self_state.json` between the two checks. The Director created a synthesis task for experiments that were already being synthesized.

## Impact

- Silent duplicate synthesis task
- Worker slot wasted on already-synthesized experiments
- Risk of concurrent writes to `self_state.json`

## Prevention

1. **Read `self_state.json` ONCE** at the start of the pass and cache it locally
2. **After creating a synthesis task**, check its status immediately — if done, the experiments are already synthesized
3. **Do NOT re-check** `self_state.json` for unsynthesized count after creating the synthesis task — use the cached count
4. **If the synthesis task completes** during the Director pass, note it in the audit log but do not create a second synthesis task

## Detection

After creating a synthesis task:
```bash
hermes kanban show <synthesis_task_id> --json 2>&1 > /tmp/synth_status.json
```

Check in a separate command:
```python
import json, datetime
d = json.load(open('/tmp/synth_status.json'))
task = d.get('task', {})
status = task.get('status', '?')
created = task.get('created_at', 0)
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
age_min = (now - created) / 60 if created else 0
print(f"Status: {status}, Age: {age_min:.0f}m")
if status == 'done':
    print("ALREADY DONE — do not create second synthesis task")
```

## Relationship to Other Pitfalls

- **Duplicate synthesis task in same Director pass (Cycle #161)**: This pitfall is about forgetting a synthesis task exists. The race condition is about the unsynthesized count changing between checks. Both lead to duplicate synthesis tasks, but with different root causes.
- **Comment race condition (Cycle #188)**: When adding experiments to a running synthesis task via `kanban_comment`, the task may complete before the comment arrives. This is a similar timing issue but at the comment level, not the unsynthesized detection level.
