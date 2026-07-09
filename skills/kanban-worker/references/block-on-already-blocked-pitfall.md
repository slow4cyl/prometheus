# `hermes kanban block` on Already-Blocked Tasks

**Added: Cycle #219**

## Symptom

When running `hermes kanban block t_xxxxxxxx "reason"` on a task that is already in `blocked` status, the CLI outputs:

```
cannot block t_xxxxxxxx
```

This looks like a failure — the command returned a non-success message and the task wasn't changed.

## Root Cause

The task is already in the desired state (`blocked`). The CLI treats "already in target state" as a non-error informational message, but formats it identically to actual failures.

## Verification

```bash
hermes kanban show t_xxxxxxxx --json 2>&1 > /tmp/task_check.json
python3 -c "import json; d=json.load(open('/tmp/task_check.json')); print(d.get('task',{}).get('status','?'))"
# Expected: "blocked"
```

## What NOT to Do

- Do NOT retry the block command
- Do NOT attempt `kanban reclaim` as a workaround (reclaim only works on `running` tasks — see separate pitfall)
- Do NOT attempt `kanban archive` unless you actually want to remove the task permanently

## Context

This is the mirror of the existing `hermes kanban unblock` misleading error pitfall:
- `unblock` on a non-blocked task → appears to fail but succeeds
- `block` on an already-blocked task → appears to fail because it IS already in the target state

Both are cosmetic CLI issues. Always verify with `kanban show --json` rather than trusting the CLI exit message.
