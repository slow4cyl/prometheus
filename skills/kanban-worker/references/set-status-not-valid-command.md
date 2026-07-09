# `set-status` Is Not a Valid Kanban Command

**Date:** Cycle #246 (June 3, 2026)
**Symptom:** `hermes kanban set-status t_fe25ba07 todo` fails with:
```
error: argument kanban_action: invalid choice: 'set-status' (choose from init, boards, create, swarm, list, ls, show, assign, reclaim, reassign, diagnostics, diag, link, unlink, claim, comment, complete, edit, block, schedule, unblock, promote, archive, tail, dispatch, daemon, watch, stats, notify-subscribe, notify-list, notify-unsubscribe, log, runs, heartbeat, assignees, context, specify, decompose, gc)
```

## Root Cause

The kanban CLI does not have a `set-status` command. Status changes are implicit in other commands:
- `reclaim` → sets status to `todo` (releases the worker claim)
- `complete` → sets status to `done`
- `block` → sets status to `blocked`
- `unblock` → sets status from `blocked` to previous status
- `promote` → sets status from `blocked` to `ready`
- `dispatch` → sets status from `ready`/`todo` to `running`

## Correct Pattern for Reclaiming Stuck Tasks

```bash
# WRONG: set-status doesn't exist
hermes kanban set-status t_fe25ba07 todo

# CORRECT: reclaim releases the worker claim and resets to todo
hermes kanban reclaim t_fe25ba07
```

## Why This Matters

When a task is stuck (running >20min with no heartbeats), the Director needs to:
1. Reclaim the task (releases worker claim, sets to todo)
2. Reassign to a different worker OR let dispatch pick it up

The `reclaim` command does step 1. There is no separate "set status to todo" command.

## Valid Status Transitions

| Command | From Status | To Status |
|---------|-------------|-----------|
| `reclaim` | `running` | `todo` |
| `complete` | `running` | `done` |
| `block` | `ready`/`running` | `blocked` |
| `unblock` | `blocked` | `ready` |
| `promote` | `blocked` | `ready` |
| `dispatch` | `ready`/`todo` | `running` |
| `claim` | `ready`/`todo` | `running` |

## Prevention

When the Director needs to change a task's status, check the valid command list:
`init, boards, create, swarm, list, ls, show, assign, reclaim, reassign, diagnostics, diag, link, unlink, claim, comment, complete, edit, block, schedule, unblock, promote, archive, tail, dispatch, daemon, watch, stats, notify-subscribe, notify-list, notify-unsubscribe, log, runs, heartbeat, assignees, context, specify, decompose, gc`

There is no `set-status`, `update`, or `modify` command.
