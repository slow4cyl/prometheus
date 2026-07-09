# Reclaim/Block Status Edge Cases

## Problem

`hermes kanban reclaim` and `hermes kanban block` have undocumented status requirements that cause silent failures when called on tasks in unexpected states.

## Status Matrix

| Task Status | `reclaim` | `block` | `unblock` |
|-------------|-----------|---------|-----------|
| `running`   | ✅ Works  | ❌ "cannot block (not blocked/scheduled?)" | ❌ N/A |
| `blocked`   | ❌ Silent no-op | ❌ Already blocked | ✅ Works |
| `ready`     | ❌ "not running or unknown id" | ❌ "not blocked/scheduled?" | ❌ N/A |
| `todo`      | ❌ "not running or unknown id" | ❌ "not blocked/scheduled?" | ❌ N/A |
| `done`      | ❌ Silent no-op | ❌ "cannot block" | ❌ N/A |

## Key Observations

1. **`reclaim` on `todo`/`ready`**: Returns error `cannot reclaim t_xxx (not running or unknown id)`. The task exists but isn't in `running` status. The error message is misleading — it says "unknown id" when the real issue is wrong status.

2. **`block` on `todo`**: Returns `cannot block t_xxx (not blocked/scheduled?)`. A newly created task in `todo` status (e.g., child of another task) cannot be blocked. The only way to prevent dispatch is to not create it in the first place.

3. **`reclaim` on `blocked`**: Silently does nothing. The task stays blocked. This is the most common failure mode — scripts call reclaim on blocked tasks expecting them to become available.

4. **`reclaim` on `done`**: Silently does nothing. Task is already complete.

## Director Implications

When a Director creates a child task (via `--parent`) and later wants to cancel it:
- If the child is still `todo` (waiting for parent to complete): neither reclaim nor block works. Options:
  - Leave it — it will dispatch when the parent completes
  - Archive if available: `hermes kanban archive <task_id>` (if supported)
- If the child was dispatched and is `running`: reclaim + block works
- If the child is `blocked`: unblock + reclaim + block (three-step)

## Real-World Example (Cycle #248)

Created `exp_1338` as child of synthesis task `t_73bd3044`. Task was in `todo` status. Attempted to reclaim and block to cancel — both failed silently. Resolution: left the task in place; it will dispatch automatically after synthesis completes (<2 min). This is actually acceptable behavior for dependent tasks.

## Prevention

Before creating child tasks, verify the parent won't create unwanted cascading effects. If a child task needs to be cancellable, ensure the parent completes quickly or use a different dependency pattern.
