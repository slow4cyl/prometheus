# Zombie Multi-PID Cleanup and Dispatch Re-dispatch (added Cycle #260)

## Multi-PID Zombie Cleanup

A single zombie task typically has **multiple PIDs** — the hermes agent process plus child subprocesses it launched (Python scripts, background heartbeat writers, shell wrappers, etc.). When killing zombies, iterate ALL PIDs found for each task, not just the first match.

**Observed in Cycle #260:** 6 zombie tasks produced 13 PIDs (avg ~2 per task). Killing only the main PID leaves orphaned children that continue consuming CPU and memory.

### Pattern

```python
import subprocess, re

result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
zombie_tids = {'t_xxx', 't_yyy', ...}  # identified zombie task IDs

for line in result.stdout.split('\n'):
    for tid in zombie_tids:
        if tid in line:
            parts = line.split()
            if len(parts) > 1:
                pid = parts[1]
                subprocess.run(['kill', pid], capture_output=True, timeout=5)
                print(f"Killed PID {pid} for task {tid}")
            break  # one PID per line, but same task may appear on multiple lines
```

**Key insight:** `ps aux` outputs one line per process. A single task ID can appear on multiple lines (agent process + subprocesses). The `break` only exits the inner loop (task matching), so the outer loop continues to find additional PIDs for the same task.

## Dispatch Re-dispatches Killed Zombies

After killing zombie processes, running `hermes kanban dispatch` will detect the now-dead processes as "Crashed" and re-dispatch those tasks.

**Why this happens:** The dispatcher checks process liveness for tasks with status=running. When the process is dead (killed), the dispatcher treats it as a crash and spawns a new worker.

**Observed in Cycle #260:** Killing 2 zombie tasks (exp_905, exp_906) caused dispatch to report:
```
Crashed: 2
  t_9026e551, t_fb3be259
Spawned: 3
  - t_9026e551 -> prometheus-worker-1
  - t_fb3be259 -> prometheus-worker-7
  - t_c928e73c -> prometheus-synthesis
```

### Decision tree

| Situation | Action |
|:----------|:-------|
| Task is truly done, should NOT re-run | Block AFTER killing zombies, BEFORE dispatch |
| Task hypothesis is still valid, re-run is acceptable | Let dispatch re-spawn naturally |
| Task is blocked, process is zombie | Kill process only — blocked status prevents re-dispatch |

### Prevention

If you want to clean up zombies WITHOUT triggering re-dispatch, block the tasks first:
```bash
hermes kanban block <task_id> "zombie cleanup: process was lingering after task completion"
# THEN kill the processes
kill <PID>
```

This way the dispatcher sees status=blocked (not running) and won't re-dispatch.
