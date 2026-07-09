# Reclaim/Block Behavior on Non-Running Tasks

Added: 2026-06-02

## Context

When a Director or Janitor tries to reclaim or block tasks that aren't in `running` status, the CLI produces error messages that can be confusing. This reference documents the exact behavior.

## Reclaim Behavior

`hermes kanban reclaim <task_id>` only works on tasks with status=running.

- **Running task**: Claim released, status changes to ready. Worker can re-dispatch.
- **Blocked task**: Silently does nothing. Task stays blocked. No error message.
- **Done task**: Shows error `"cannot reclaim [id] (not running or unknown id)"`. This is correct behavior — the task is already terminal.
- **Ready task**: Shows error. Task is already ready for dispatch.
- **Unknown task ID**: Shows error. Task doesn't exist.

## Block Behavior

`hermes kanban block <task_id> "reason"` only works on running tasks.

- **Running task**: Task moves to blocked status with the given reason.
- **Done task**: Shows error `"cannot block [reason]"`. This is correct — the task is already terminal.
- **Already blocked task**: Shows error. Task is already blocked.
- **Ready task**: May work depending on implementation.

## Common Failure Scenarios

### Status Desync (Director sees done task as running)

The most common scenario: `kanban list --status running --json` shows a task as running, but `kanban show <id> --json` reveals it's actually done. This is documented in the "Status desync" pitfall.

When you encounter this:
1. The reclaim/block commands will fail with the errors above
2. This is NOT a bug — the commands correctly refuse to operate on non-running tasks
3. Verify with `kanban show --json` before assuming a task is stuck
4. No action needed — the task is already complete

### Stuck Worker on Done Task

Sometimes a worker process lingers after its task completes (zombie process). The task shows done in kanban, but ps aux shows a live process.

- The process is a zombie, not evidence of ongoing work
- Task is complete regardless of process state
- Optionally kill the process: `kill <PID>`
- Do NOT try to reclaim or block — the task is already done

## Error Message Interpretation

| Command | Task Status | Error Message | Meaning |
|---------|-------------|---------------|---------|
| reclaim | done | "cannot reclaim [id] (not running or unknown id)" | Correct — task is terminal |
| reclaim | blocked | (silently does nothing) | Correct — use unblock instead |
| block | done | "cannot block [reason]" | Correct — task is terminal |
| block | already blocked | "cannot block [reason]" | Correct — already blocked |

These errors are NOT bugs. They indicate the task is not in a state where the operation makes sense.
