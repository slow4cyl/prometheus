# Orphaned Dual-Task Detection (June 2026)

## Problem

Workers can start a new task before marking the old one as done. The dispatcher spawns a new worker process for the same profile, leaving the old task in "running" with an alive PID but no active work. Result: 26 "running" tasks but only 50 workers, with orphaned tasks consuming kanban slots.

## Detection

Map each worker profile's PID to its kanban task via `ps aux`:

```bash
ps aux | grep "prometheus-worker" | grep -v grep | grep "kanban task"
```

This shows which profile is running which task. If a profile appears with 2+ task IDs, the older task is orphaned.

```python
import subprocess, re, sqlite3

result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
worker_tasks = {}
for line in result.stdout.split('\n'):
    if 'prometheus-worker' in line and 'kanban task' in line:
        m_prof = re.search(r'prometheus-worker-(\d+)', line)
        m_task = re.search(r'kanban task (t_\w+)', line)
        if m_prof and m_task:
            prof = int(m_prof.group(1))
            task = m_task.group(1)
            worker_tasks.setdefault(prof, []).append(task)

orphans = []
for prof, tasks in worker_tasks.items():
    if len(tasks) > 1:
        orphans.extend(tasks[1:])  # first is current, rest are orphaned
        print(f"Worker-{prof}: {tasks}")
```

## Resolution

1. **Check if orphaned task has output** — if experiment JSON files exist in `~/.hermes/experiments/`, the work was done; just needs completion
2. **If no output and >30 min old** — reclaim: `hermes kanban reclaim <task_id>`
3. **If no output and <30 min old** — might still be in progress from a previous dispatch cycle; wait or reclaim

## Prevention

The Director should check for dual-task workers BEFORE creating new tasks:

```bash
# Count tasks per profile
ps aux | grep "prometheus-worker" | grep -v grep | grep "kanban task" | \
  grep -oP "prometheus-worker-\d+" | sort | uniq -c | sort -rn
```

If any profile has 2+ tasks, reclaim the older one before creating new tasks.

## Related

- `pitfalls-all.md` — zombie worker accumulation pattern
- `zombie-multi-pid-and-dispatch-rerun.md` — multi-PID cleanup
