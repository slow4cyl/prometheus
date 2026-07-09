# Reclaim Failure Recovery Pattern (added Cycle #210)

## The Problem

When a task shows `status=running` in `kanban show --json` but:
- `hermes kanban reclaim <id>` fails with "cannot reclaim (not running or unknown id)"
- `hermes kanban block <id> "reason"` fails with "cannot block"

...the task is in a **hidden blocked state** — the show endpoint is desynced from the actual state. The task was likely blocked on a prior attempt (crash, timeout, explicit block), but the status field didn't update correctly.

## Recovery Sequence

```
1. kanban reclaim <id>  →  fails ("not running or unknown id")
2. kanban block <id>    →  fails ("cannot block")
3. kanban unblock <id>  →  succeeds
4. kanban show <id> --json  →  verify status changed to "ready"
5. hermes kanban dispatch  →  re-spawns a worker
```

## Why This Happens

The kanban database has multiple status-adjacent fields (blocked_reason, worker_claim, etc.) that can desync from the primary `status` field. When a task is blocked:
- `kanban list --json` may still show it as `running` (list-view desync, documented separately)
- `kanban show --json` may also show `running` if the status field wasn't atomically updated
- `reclaim` checks the worker_claim field and finds no claim (it was released on block)
- `block` checks the status and finds it already has a block event
- `unblock` clears the blocked_reason and promotes to ready — it works because it's the only operation that handles the blocked→ready transition

## Detection

If `kanban show --json` says `status=running` but:
- `ps aux` shows NO process for the task, AND
- Both `reclaim` and `block` fail

...suspect the hidden-blocked state. The task likely crashed on a prior attempt and the block event was recorded but the status field desynced.

## Contrast with Standard Reclaim

| Scenario | reclaim | block | unblock | Correct action |
|---|---|---|---|---|
| Task running, process alive | ✅ releases claim | ❌ "already running" | ❌ not blocked | Reclaim to re-dispatch |
| Task running, process dead | ✅ releases claim | ✅ blocks it | ❌ not blocked | Reclaim + block to prevent re-dispatch |
| Task secretly blocked | ❌ "not running" | ❌ "already blocked" | ✅ clears block | Unblock to re-dispatch |
| Task done | ❌ "not running" | ❌ "already done" | ❌ not blocked | Leave alone or create new task |

## Related Pitfalls

- Status desync between `kanban list` and `kanban show` (see main SKILL.md)
- `kanban block` will fail with "cannot block" for tasks that are already done
- `reclaim` only works on `running` tasks, not `blocked`
