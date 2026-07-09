# Ready Task Reassignment Pitfall (added Cycle #228)

## Problem

Tasks created with `worker-N` instead of `prometheus-worker-N` as assignee sit in `ready` forever. The dispatcher skips them with "Skipped (non-spawnable assignee — terminal lane, OK)" because it can't find a matching profile.

## Detection

```bash
hermes kanban list --status ready --json > /tmp/kanban_ready.json
```

If ready tasks exist but `hermes kanban dispatch` reports 0 spawns (or skips them), check assignees:

```bash
hermes kanban show <task_id> --json | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('task', {}).get('assignee', '?'))
"
```

If assignee is `worker-N` (not `prometheus-worker-N`), the task is non-spawnable.

## Fix

```bash
# Reassign to correct profile
hermes kanban assign <task_id> prometheus-worker-<N>

# Then dispatch
hermes kanban dispatch
```

## Root Cause

Previous Director passes created tasks with short profile names (`worker-N`) instead of the full `prometheus-worker-N`. The tasks persist in `ready` across cycles until someone reassigns them.

## Prevention

Always use the full profile name when creating tasks:
- ✅ `prometheus-worker-3`
- ❌ `worker-3`

## Interaction with Saturation

During saturation (≥7 running tasks), reassigned ready tasks can fill free workers alongside new experiment tasks. The saturation exception (free_workers ≥ 3 AND 3+ OPEN queue items) applies to both new tasks AND reassigned ready tasks.

## Example (Cycle #228)

1. Board had 21 running tasks, 3 free workers
2. Found 6 ready tasks with `worker-N` assignee (non-spawnable)
3. Reassigned all 6 to `prometheus-worker-N`
4. Created 3 new experiment tasks (saturation exception)
5. Dispatched all 9 — all spawned successfully
6. Final state: 28 running tasks, 0 free workers
