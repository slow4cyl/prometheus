# Reclaimed Task Process Cleanup (Cycle #247)

## Problem

After reclaiming a task via `kanban reclaim`, the OS process continues running independently. The kanban task status changes to "ready" but the subprocess (Python script, hermes agent, etc.) is not terminated.

## Impact

1. **Worker profile appears busy:** The dispatcher sees the worker profile as "occupied" by the zombie process, preventing new tasks from being assigned to it.
2. **API budget waste:** The orphaned process continues making API calls on work nobody will read.
3. **Board state confusion:** `kanban list --status running` may still show the task as running due to list-view desync.

## Detection

After reclaiming a task:

```bash
# Check for orphaned processes
ps aux | grep "<task_id>" | grep -v grep

# Or check by worker profile
ps aux | grep "prometheus-worker-<N>" | grep -v grep
```

## Cleanup

```bash
# Kill processes for a specific task
ps aux | grep "<task_id>" | grep -v grep | awk '{print $2}' | xargs kill

# Or kill all orphaned processes for reclaimed tasks
for task_id in t_xxx t_yyy t_zzz; do
    pids=$(ps aux | grep "$task_id" | grep -v grep | awk '{print $2}')
    if [ -n "$pids" ]; then
        echo "Killing processes for $task_id: $pids"
        echo "$pids" | xargs kill 2>/dev/null
    fi
done
```

## Prevention

When reclaiming multiple tasks, always follow the three-step pattern:

1. `hermes kanban reclaim <task_id>` — releases the kanban claim
2. `kill <PID>` — terminates the OS process
3. `hermes kanban block <task_id> "reason"` — prevents re-dispatch (optional, if task has underlying issues)

## Real-World Example (Cycle #247)

6 tasks with wrong experiment IDs (exp_1/2/3/4) were reclaimed. The kanban status changed to "blocked" but zombie processes continued for workers 2, 5, 7, 9, 11, 15. These profiles appeared busy, preventing 7 ready tasks from being dispatched. Manual process cleanup freed the profiles and allowed new task creation.

## Relationship to Other Pitfalls

- **Zombie worker accumulation (Cycle #184):** Workers on completed tasks can linger as zombie processes. Same cleanup pattern applies.
- **Hung task lifecycle (Cycle #190):** reclaim → kill → block for tasks that are stuck. The kill step is what this document emphasizes.
