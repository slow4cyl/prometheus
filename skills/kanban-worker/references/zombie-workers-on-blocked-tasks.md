# Zombie Workers on Blocked Tasks (added June 3, 2026)

## Problem

When the Director blocks a task (e.g., due to duplicate ID, topic overlap, or other issues), the worker process that was running that task continues executing. The worker doesn't immediately detect the block and keeps consuming resources (CPU, API calls) on a task that will never be completed.

## Example (June 3, 2026)

1. Director blocks task t_fe1e3723 (duplicate exp_2964)
2. Worker process 2024647 (prometheus-worker-19) continues running t_fe1e3723
3. Worker process 2024648 (prometheus-worker-20) continues running t_d8512aa2 (also blocked)
4. Both workers waste API budget on tasks that will never be completed

## Detection

After blocking tasks, check for zombie workers:

```bash
# For each blocked task, check if worker processes are still running
for task_id in <blocked_task_ids>; do
    echo "=== $task_id ==="
    ps aux | grep "$task_id" | grep -v grep
done
```

Or in Python:

```python
import subprocess, re

blocked_tasks = ['t_fe1e3723', 't_d8512aa2']  # tasks just blocked

result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
for line in result.stdout.split('\n'):
    for task_id in blocked_tasks:
        if task_id in line:
            print(f"ZOMBIE: {task_id} still has running process")
            # Extract PID
            parts = line.split()
            pid = parts[1]
            print(f"  PID: {pid}")
```

## Fix

Kill the zombie worker processes:

```bash
# Kill specific PIDs
kill 2024647 2024648

# Or kill all workers on blocked tasks
for task_id in <blocked_task_ids>; do
    pids=$(ps aux | grep "$task_id" | grep -v grep | awk '{print $2}')
    if [ -n "$pids" ]; then
        echo "Killing workers for $task_id: $pids"
        kill $pids
    fi
done
```

## Why Workers Don't Detect Blocks Immediately

1. **Lease check interval**: Workers only check task status at phase transitions (every few minutes), not continuously
2. **API call latency**: If a worker is mid-API call, it won't check status until the call completes
3. **No block signal**: The kanban system doesn't send a signal to running workers when their task is blocked

## Prevention

1. **After blocking tasks, always check for and kill zombie workers**
2. **Consider blocking BEFORE dispatch** (if you know a task will be blocked) to avoid wasting worker startup time
3. **Log zombie kills in audit trail** for pattern tracking

## Related Pitfalls

- `reclaimed-task-process-cleanup-cycle247.md` — Similar pattern for reclaimed tasks
- `zombie-multi-pid-and-dispatch-rerun.md` — Multiple PIDs per worker
- This file: Zombies on BLOCKED tasks (distinct from reclaimed tasks)
