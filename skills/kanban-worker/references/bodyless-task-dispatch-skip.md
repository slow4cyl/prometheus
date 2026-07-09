# Bodyless Task Dispatch Skip (June 2026)

## Problem

Tasks created with `body=NULL` or `body=''` are silently skipped by the dispatcher — no error, no event, just `Spawned: 0`. The Director wastes time trying to dispatch a task that can never spawn.

## Detection

When `hermes kanban dispatch` returns 0 spawned for assigned ready tasks with free workers:

```sql
-- Check if ready tasks have bodies
SELECT id, title, assignee, body IS NULL as no_body, length(body) as body_len
FROM tasks WHERE status='ready';
```

If `no_body = 1` or `body_len = 0`, that's the blocker.

## Why This Happens

The dispatcher's `_default_spawn` function reads the task body to construct the worker's prompt. A bodyless task would produce a worker with no instructions — the dispatcher silently skips rather than spawning a useless worker.

This is distinct from:
- **Unassigned tasks** — skipped because no worker is specified
- **Profile-capped tasks** — skipped because the worker already has a running task
- **Respawn-guarded tasks** — skipped because of recent failure/success

## Fix

Either cancel the bodyless task or add a body before dispatch:

```sql
-- Option 1: Cancel (if the task is unrecoverable)
UPDATE tasks SET status='cancelled' WHERE id='TASK_ID' AND body IS NULL;

-- Option 2: Add body (if you can reconstruct it)
UPDATE tasks SET body='...' WHERE id='TASK_ID';
```

Then re-dispatch.

## Common Causes

1. **Reclaimed stuck tasks** — Task was reclaimed after the worker died, but the body was lost or never persisted
2. **Batch creator edge case** — `batch_create_tasks.py` occasionally creates tasks with empty bodies when queue item text is malformed
3. **Manual creation** — `hermes kanban create` without `--body` flag

## Real-World Example (June 2026)

Task t_d8ac6ecf (exp_3587) was reclaimed after 227 minutes stuck. Body was NULL. Dispatcher returned 0 spawned for this task despite free workers. Detection: `body IS NULL = 1`. Fix: cancelled the task.

## Prevention

Always verify task body after creation:

```bash
# Quick check after batch creation
sqlite3 ~/.hermes/kanban.db "SELECT id, body IS NULL FROM tasks WHERE status='ready';"
```
