# Ready-With-Active-Worker Desync (June 2026)

## Problem

A task can be in `status='ready'` while its assigned worker process is alive and actively running the experiment. The dispatcher skips it (profile cap or respawn guard), but the worker continues running. This creates a paradox: the task looks available for dispatch but is actually in use.

## How It Happens

1. Worker claims task → status set to 'running', worker_pid recorded
2. Gateway dispatch creates a NEW task for the same worker (different task_id)
3. Original task's status reverts to 'ready' (claim expired or was never fully promoted)
4. But the worker process is still running the original experiment
5. The new task gets dispatched, but the original task sits in 'ready' with a live worker

## Detection

```sql
-- Check ready tasks with worker_pids
SELECT id, title, assignee, worker_pid 
FROM tasks WHERE status='ready' AND worker_pid IS NOT NULL;
```

Then verify if the worker_pid is alive:

```bash
ps -p WORKER_PID -o pid,cmd 2>/dev/null || echo "Process is DEAD"
```

If the process is alive, the task is actually running despite the 'ready' status.

## Fix

```sql
-- Mark as running since the worker is actively executing it
UPDATE tasks SET status='running' 
WHERE id='TASK_ID' AND status='ready' AND worker_pid IS NOT NULL;
```

Or if the worker has moved on to a different task:

```sql
-- Clear the stale worker reference
UPDATE tasks SET worker_pid=NULL, claim_lock=NULL 
WHERE id='TASK_ID' AND status='ready';
```

## Distinction from Other Desyncs

| Pattern | Task Status | Process | Fix |
|---------|-------------|---------|-----|
| List shows done as running | running | dead | Reclaim or mark done |
| Ready-with-active-worker | ready | alive | Mark as running |
| Stale claim lock | ready | dead | Clear claim_lock |
| Claim lock on gateway PID | ready | alive (gateway) | Clear claim_lock + reassign |

## Real-World Example (June 2026)

Task t_86433a89 was in 'ready' status, assigned to prometheus-worker-20. But:
- worker_pid=3391747 (prometheus-worker-40) was alive and running the experiment
- claim_lock was set to 'user:3370512' (the gateway process, also alive)
- The dispatcher skipped it because of the claim lock
- Fix: cleared claim_lock, reassigned to worker-20, dispatcher spawned new worker

## Prevention

After reclaiming stuck tasks, always check if the worker process is still alive before reassigning. If alive, the task is actually running — don't reclaim it.
