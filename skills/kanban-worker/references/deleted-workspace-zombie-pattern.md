# Deleted Workspace Directory While Process Alive (Cycle #244)

## Pattern

A worker process is alive (`ps aux` confirms) but its workspace directory has been deleted — the process is running in a phantom directory.

## Detection

1. `ps aux | grep <task_id>` → process alive
2. `ls -la /proc/PID/cwd` → shows path with `(deleted)` suffix
3. `kanban show <id> --json` → status is `done`

Example output:
```
lrwxrwxrwx 1 user user 0 Jun  2 10:37 /proc/150991/cwd -> ~/.hermes/kanban/workspaces/t_44618135 (deleted)
```

## Why It Happens

The kanban workspace GC runs independently of worker processes. If a task completes via `kanban_complete` but the hermes process hasn't fully exited, GC can delete the workspace while the process lingers. The process continues running but is effectively dead — it can't produce output or respond to signals meaningfully.

## Distinguishing From Other Patterns

| Pattern | Process | Workspace | Task Status | Action |
|---------|---------|-----------|-------------|--------|
| **Deleted workspace** | Alive | `(deleted)` | `done` | Kill process |
| Stuck worker | Alive | Exists, stale | `running` | Block task |
| Crashed worker | Dead | May exist | `running` | Block task |
| Zombie (normal) | Alive | Exists | `done` | Kill process (harmless) |

## Action

Kill the zombie process (`kill <PID>`). The task is already done — no reclaim needed. `kanban reclaim` fails on `done` tasks.

## Real Example (Cycle #244)

Task t_44618135 ("C-extension pipeline caps at 1.61x") had:
- Process PID 150991 alive (21m runtime, 2s CPU)
- Workspace directory deleted
- Task status: `done` (but 0 events — status desync)
- `kanban reclaim` failed: "cannot reclaim (not running or unknown id)"

Resolution: Task was already complete. Process was a zombie from workspace GC. No action needed beyond noting it in the Director pass log.
