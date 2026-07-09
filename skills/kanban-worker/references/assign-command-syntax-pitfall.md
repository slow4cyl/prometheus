# `hermes kanban assign` Command Syntax Pitfall

**Date:** Cycle #245 (June 3, 2026)
**Symptom:** `hermes kanban assign <task_id> --worker <profile>` fails with "usage: hermes [-h]..." error. 26 consecutive failures.
**Root cause:** `assign` takes positional arguments, not `--worker` flag.

## Correct Syntax

```bash
hermes kanban assign <task_id> <profile>
```

**Examples:**
```bash
hermes kanban assign t_abc123 prometheus-worker-5
hermes kanban assign t_abc123 prometheus-synthesis
hermes kanban assign t_abc123 none          # unassign
```

## Incorrect Syntax (Causes Error)

```bash
hermes kanban assign t_abc123 --worker prometheus-worker-5   # WRONG
hermes kanban assign t_abc123 --profile prometheus-worker-5  # WRONG
```

## Help Output

```
usage: hermes kanban assign [-h] task_id profile

positional arguments:
  task_id
  profile     Profile name (or 'none' to unassign)
```

## Impact

When creating tasks in bulk (e.g., 15+ tasks for free workers), hitting this syntax error on every assignment wastes significant time. The Director must know the correct syntax before the assignment loop begins.

## Prevention

Always use positional arguments:
```python
cmd = ['hermes', 'kanban', 'assign', task_id, worker]  # CORRECT
# NOT:
cmd = ['hermes', 'kanban', 'assign', task_id, '--worker', worker]  # WRONG
```
