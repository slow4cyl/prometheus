# Synthesis Scope Expansion — 0-Events Check Pattern

## Problem
The "Revised rule (Cycle #246)" in SKILL.md says "ALWAYS create a separate synthesis task" when expanding scope after dispatch. This is overly conservative — when the synthesis worker hasn't started processing yet, it's safe to comment.

## The 0-Events Check
After dispatching a synthesis task, check the worker's event count:

```bash
hermes kanban show <task_id> --json 2>&1 > /tmp/synth_check.json
python3 -c "
import json
d = json.load(open('/tmp/synth_check.json'))
events = d.get('task', {}).get('events', []) or []
print(f'Events: {len(events)}')
"
```

**Decision:**
- **0 events**: Worker hasn't started processing. Safe to `kanban_comment` with additional experiments. The worker will read the comment thread when it starts.
- **1+ events**: Worker has started processing. Create a separate synthesis task to avoid the race condition.

## Verified Example (Director Pass, Cycle #245)
1. Created synthesis task `t_dcceae0e` for exp_1490, 1502, 1504, 1508
2. Dispatched successfully (1 spawn)
3. During the same pass, exp_1511 and exp_1513 completed
4. Checked `t_dcceae0e` → 0 events (worker not yet started)
5. Added exp_1511 and exp_1513 via `kanban_comment`
6. Synthesis worker processed all 6 experiments correctly

## Why This Works
The race condition occurs when the synthesis worker has already read `self_state.json` and started writing. At 0 events, the worker hasn't even started its process — it will read the comment thread fresh when it boots.

## When to Use Separate Task Instead
- Worker has 1+ events (has started processing)
- Worker has heartbeats (actively working)
- You're unsure about the worker's state
- The synthesis task is nearly complete (check `latest_summary` for partial results)

## Reference
This pattern supplements the "Revised rule" in SKILL.md and the "Comment race condition" pitfall. The canonical source for the 0-events check is this file.
