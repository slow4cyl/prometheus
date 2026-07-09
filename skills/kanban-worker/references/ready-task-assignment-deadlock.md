# Ready-Task Assignment Deadlock (Cycle #250)

## Problem

`kanban dispatch` only spawns workers for ready tasks whose `assignee` field matches a free worker. If all ready tasks are assigned to busy workers, dispatch spawns 0 even when free workers exist.

## Detection

```
hermes kanban list --status ready --json  →  shows 7 tasks
hermes kanban dispatch                    →  Spawned: 0
ps aux | grep prometheus-worker           →  shows 2+ free workers
```

## Root Cause

Tasks inherit their assignee from creation time. When a worker finishes its task and becomes free, the ready queue still has tasks assigned to the OLD busy workers. The dispatcher won't reassign — it only spawns for matched assignees.

## Fix

1. Get free workers: `ps aux | grep prometheus-worker | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u` → subtract from full set (1-22)
2. Get ready task assignees: `hermes kanban list --status ready --json`
3. Cross-reference: find ready tasks assigned to free workers (these will spawn)
4. Reassign 1-2 ready tasks to remaining free workers: `hermes kanban assign <task_id> prometheus-worker-N`
5. Re-dispatch: `hermes kanban dispatch`

## Worked Example (Cycle #250)

- 20 running, 2 free workers (3, 11)
- 7 ready tasks assigned to: 3, 4, 7, 9, 11, 14, 15, 16
- Worker 3 free → t_7182dd76 already assigned to worker-3 ✓
- Worker 11 free → t_f899ec88 already assigned to worker-11 ✓
- But dispatch still spawned 0 (spillover — scheduler picks up on next tick)
- Reassigning additional ready tasks to free workers ensures scheduler picks them up faster

## Rule

**Do NOT create new tasks to fill free workers when ready tasks exist** — reassign the existing ready tasks instead. Creating new tasks when ready tasks are waiting wastes the ready-task slot and adds to queue bloat.
