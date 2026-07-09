# Stale Claim Lock Prevents Dispatch (June 2026)

## The Problem

A kanban task in `ready` state with an assigned worker fails to dispatch silently — the dispatcher skips it without error. Root cause: a stale `claim_lock` from a dead process blocks re-assignment.

## Detection

When a task is in `ready` state with a valid assignee but `hermes kanban dispatch` doesn't spawn it:

```sql
-- Check claim lock state
sqlite3 ~/.hermes/kanban.db "
SELECT id, title, status, assignee, claim_lock, claim_expires, worker_pid
FROM tasks WHERE id='TASK_ID'
"
```

If `claim_lock` is set and `worker_pid` points to a dead process:

```bash
# Verify process is dead
ps -p PID -o pid,comm 2>/dev/null || echo "Process is DEAD"
```

## Fix

Release the stale claim lock:

```sql
sqlite3 ~/.hermes/kanban.db "
UPDATE tasks SET claim_lock=NULL, claim_expires=NULL
WHERE id='TASK_ID'
"
```

Then re-dispatch:

```bash
hermes kanban dispatch
```

## Why This Happens

1. Worker claims task → `claim_lock` set, `worker_pid` recorded
2. Worker process dies (crash, OOM, gateway restart)
3. Task status may revert to `ready` (if claim expired or was never fully promoted)
4. But `claim_lock` field persists — the dispatcher sees it as "claimed" and skips

This is distinct from the "reclaimed tasks with live processes" pitfall — here the process is dead but the lock remains.

## Relation to Other Pitfalls

- **Stuck worker dead process false positive** (`stuck-worker-dead-process-false-positive.md`): That pitfall is about blocking tasks with dead processes. This is about `ready` tasks that won't dispatch due to stale locks.
- **Reclaimed task process cleanup** (`reclaimed-task-process-cleanup-cycle247.md`): That covers killing live processes after reclaim. This covers the database cleanup when the process is already dead.

## Real-World Example (June 2026)

Synthesis task t_26963b9b was in `ready` state, assigned to `prometheus-synthesis`. Dispatch returned 0 spawned. Investigation:
- `claim_lock = 'user:1528236'`, `worker_pid = 1528449`
- `ps -p 1528449` → process dead
- Released claim lock → dispatch spawned immediately

## Prevention

The dispatcher should auto-clear stale claim locks when:
- `claim_expires` < current timestamp AND task is in `ready` state
- OR `worker_pid` points to a dead process

Until then, Directors should check `claim_lock` when dispatching fails for assigned ready tasks.
