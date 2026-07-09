# Director Pitfall: Dispatcher Ignores Worker Busyness

**Added:** Cycle #189  
**Severity:** Medium — wastes a worker slot, causes duplicate work

## Problem

When creating a task with `--assignee prometheus-worker-N`, the kanban dispatcher assigns based on the flag without checking if that worker is already running a task. If the Director assigns two tasks to the same worker during one pass, BOTH get dispatched — the second steals the worker from the first.

## Example (Cycle #189)

```
# Director creates first task assigned to worker-1 (busy)
hermes kanban create "exp_557: ..." --assignee prometheus-worker-1
# Later creates second task, reassigns to worker-2
hermes kanban create "exp_557: ..." --assignee prometheus-worker-2
# Dispatch sends BOTH — worker-1 gets the first task despite being busy
hermes kanban dispatch
# Result: worker-1 now has 2 tasks, first one starved
```

## Detection

After dispatch, cross-reference `ps aux` worker list against newly created task assignees:

```bash
# Get busy workers
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u

# Compare against tasks you just created
```

## Fix

Reclaim + block the duplicate immediately:

```bash
hermes kanban reclaim <duplicate_task_id>
hermes kanban block <duplicate_task_id> "duplicate of <original_id> — assigned to busy worker"
```

## Prevention

1. Build a `busy_workers` set from `ps aux` BEFORE creating any tasks
2. Track assignments in a local dict during the Director pass: `assigned_workers = {}`
3. Before each `kanban_create`, check: `if worker in assigned_workers or worker in busy_workers: pick different worker`
4. Never assign the same worker twice in one pass

## Key Insight

The `--assignee` flag is a HINT to the dispatcher, not a constraint. The dispatcher will dispatch any ready task regardless of whether the assigned worker is free. The Director is responsible for ensuring worker availability — the dispatcher does not enforce it.
