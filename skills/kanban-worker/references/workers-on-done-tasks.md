# Workers on Done Tasks (June 2026)

## Problem

Worker processes can remain alive after their task is marked `done` in the kanban DB. Unlike zombies on blocked/reclaimed tasks (where the task status changed externally), these workers completed their work but didn't exit cleanly — possibly doing cleanup, retry loops, or having started a new task before the old process terminated.

## Detection

Cross-reference kanban DB status with `ps` process list:

```python
import sqlite3, subprocess

c = sqlite3.connect('~/.hermes/kanban.db'.replace('~', '~'))
r = c.execute('SELECT id, title FROM tasks WHERE status="running"').fetchall()

for tid, title in r:
    result = subprocess.run(['pgrep', '-f', f'kanban task {tid}'], capture_output=True, text=True)
    if result.returncode != 0:
        print(f'ORPHANED: {tid} — DB says running but no process')
    else:
        print(f'ACTIVE: {tid}')
```

Also check the reverse — processes on done tasks:

```bash
# Find worker processes and check their task status
ps aux | grep "kanban task" | grep -v grep | while read line; do
    task_id=$(echo "$line" | grep -oP 'kanban task \Kt_\w+')
    status=$(python3 -c "import sqlite3; c=sqlite3.connect('~/.hermes/kanban.db'); print(c.execute('SELECT status FROM tasks WHERE id=?', ('$task_id',)).fetchone()[0])")
    if [ "$status" = "done" ]; then
        pid=$(echo "$line" | awk '{print $2}')
        echo "ZOMBIE ON DONE: $task_id (PID $pid) — task is done but process alive"
    fi
done
```

## Cleanup

```bash
# Kill workers on done tasks
for tid in t_6493caa1 t_e5454b7a t_9466c38e; do
    pids=$(ps aux | grep "kanban task $tid" | grep -v grep | awk '{print $2}')
    if [ -n "$pids" ]; then
        echo "Killing workers for $tid: $pids"
        echo "$pids" | xargs kill 2>/dev/null
    fi
done
```

## When This Happens

1. **Worker completed task but cleanup phase hangs** — worker marks task done, then tries to write results or clean up workspace, and gets stuck
2. **Worker started new task before old process exited** — profile cap shows 1 task but 2 processes exist (see `orphaned-dual-task-detection.md`)
3. **API retry loop after completion** — worker finished experiment but retries a failed API call for logging/upload

## Relationship to Other Patterns

- `zombie-workers-on-blocked-tasks.md` — workers on BLOCKED tasks (task status changed externally)
- `reclaimed-task-process-cleanup-cycle247.md` — workers on RECLAIMED tasks (Director released the claim)
- `orphaned-dual-task-detection.md` — worker has 2+ tasks (old one not cleaned up)
- **This file** — workers on DONE tasks (task completed normally but process didn't exit)
