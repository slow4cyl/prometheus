# Spawn-at-Creation Behavior (Cycle #257)

## Observation

When creating a kanban task with `--assignee <profile>`, the dispatcher's create hook immediately spawns the worker process. The task transitions from `ready` to `running` before any subsequent `hermes kanban dispatch` call.

## Evidence

```
# Created synthesis task with explicit assignee
hermes kanban create "SYNTHESIS: ..." --assignee prometheus-synthesis --body "..."
# → Created t_31ffc2d5 (ready, assignee=prometheus-synthesis)

# Task immediately shows running
hermes kanban show t_31ffc2d5 --json → status: running
ps aux | grep t_31ffc2d5 → PID 138701 alive

# Subsequent dispatch reports zero spawns
hermes kanban dispatch → Spawned: 0
# (because the task was already dispatched at creation)
```

## Implication for Directors

- **Synthesis tasks**: Create with `--assignee prometheus-synthesis` → starts immediately. No need to call `dispatch` afterward for this task.
- **Batch experiment tasks**: Created without explicit `--assignee` (or with generic assignee) → stay in `ready` queue, dispatched by `hermes kanban dispatch` or the next scheduler tick.
- **Mixed batch**: If you create 5 tasks (3 with assignees, 2 without), the 3 assigned tasks start immediately and the 2 unassigned ones are picked up by `dispatch`.

## Detection

After `kanban create`, check `kanban show --json`:
- `status: running` → auto-dispatched at creation (explicit assignee triggered it)
- `status: ready` → waiting for `dispatch` call or next scheduler tick

## Why This Matters

The "Dispatch spillover" pitfall (Spawned: 0 after dispatch) is partially explained by this behavior: if tasks were created with explicit assignees, they're already running when dispatch is called. The dispatch command only handles tasks that weren't auto-dispatched at creation.
