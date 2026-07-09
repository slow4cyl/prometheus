# Cross-Platform Workspace Path Mismatch

## Problem

When a kanban database is migrated between macOS and Linux (or vice versa), tasks retain their original `workspace_path` values. On the new OS, workspace creation fails silently.

## Symptoms

- Dispatcher logs "kanban dispatcher stuck: ready queue non-empty for N consecutive ticks but 0 workers spawned"
- Ready tasks have `consecutive_failures = 1` and `last_failure_error = "workspace: [Errno 13] Permission denied: '/Users'"`
- Tasks stay in `ready` status indefinitely
- No workers are spawned despite free worker slots

## Root Cause

1. Tasks created on macOS have paths like `/Users/future/.hermes/kanban/workspaces/t_xxx`
2. After migration to Linux, `resolve_workspace()` tries to create `/Users/future/.hermes/kanban/workspaces/t_xxx`
3. Linux denies permission to create `/Users` directory
4. Error is recorded but `consecutive_failures` stays below `failure_limit` (default: 2)
5. Dispatcher retries every tick, same failure repeats

## Detection

```sql
-- Find tasks with stale OS-specific paths
SELECT id, workspace_path, consecutive_failures, last_failure_error 
FROM tasks 
WHERE status = 'ready' 
AND (workspace_path LIKE '/Users/%' OR workspace_path LIKE '/home/%')
AND last_failure_error LIKE '%Permission denied%';
```

## Fix

```sql
-- Update workspace paths (adjust old/new home as needed)
UPDATE tasks 
SET workspace_path = REPLACE(workspace_path, '/Users/future', '~'),
    consecutive_failures = 0,
    last_failure_error = NULL
WHERE workspace_path LIKE '%/Users/future%'
AND status IN ('ready', 'running');

-- Also create the workspace directories
-- (run from shell, not SQL)
```

## Prevention

When migrating kanban databases between platforms:
1. Run path correction script before dispatching
2. Or add workspace path validation in `dispatch_once()` that detects stale OS prefixes
3. Consider storing paths relative to HERMES_HOME instead of absolute paths

## Related

- `dispatch_stuck_diagnosis.md` - general dispatcher stuck diagnosis
- `zombie-worker-cleanup.md` - worker process cleanup patterns
