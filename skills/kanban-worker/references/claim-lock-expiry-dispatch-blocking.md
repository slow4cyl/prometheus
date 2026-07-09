# Claim Lock Expiry and Dispatch Blocking

**Added: Cycle #245 (June 2026)**

## Problem

Tasks can have expired claim locks that prevent `hermes kanban dispatch` from picking them up. The dispatch mechanism refuses to claim tasks with existing locks, even if the lock has expired.

## Symptoms

- `hermes kanban dispatch --dry-run` shows 0 spawned tasks despite free workers
- Tasks remain in "ready" status with assignees but no workers running
- `hermes kanban claim <task_id>` fails with "cannot claim: status=ready lock=user:PID"

## Detection

Check claim lock status on ready tasks:
```sql
SELECT id, title, claim_lock, claim_expires, 
       (strftime('%s','now') - claim_expires) as lock_age
FROM tasks 
WHERE status='ready';
```

If `lock_age` is negative (e.g., -270), the lock has expired but not been cleared.

## Manual Fix

Clear expired claim locks:
```sql
UPDATE tasks 
SET claim_lock = NULL, claim_expires = NULL
WHERE status='ready';
```

Then redispatch:
```bash
hermes kanban dispatch --max 10
```

## Root Cause

When a Director process spawns workers, it creates claim locks with a TTL (typically 900 seconds). If the worker process dies before claiming the task, the lock remains until it expires. But the dispatch mechanism may not auto-clear expired locks, leaving tasks in a "ready but unclaimable" state.

## Prevention

The dispatcher should auto-clear expired locks before attempting to claim. This could be added to the dispatch logic:
```python
# Before claiming, clear expired locks
now = int(time.time())
db.execute("""
    UPDATE tasks 
    SET claim_lock = NULL, claim_expires = NULL
    WHERE status='ready' AND claim_expires < ?
""", (now,))
```

## Impact

- Workers sit idle while tasks are "ready but locked"
- Director sees free workers but dispatch produces 0 spawns
- Manual intervention required to clear locks and redispatch

## Related

- `status-desync-detection-pattern.md` — related desync pattern
- `director-pass-skip-threshold-with-free-workers.md` — skip threshold with free workers
