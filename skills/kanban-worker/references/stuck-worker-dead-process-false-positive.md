# Stuck Worker Protocol — Process-Not-Found False Positive (added Cycle #219)

## The Bug

The Stuck Worker Protocol says:
> Process NOT found → crashed. Block immediately

This is **wrong** for completed tasks. When a worker calls `kanban_complete()` and exits normally, `ps aux` shows no process — but the task is DONE, not crashed. Blocking it creates a phantom blocked task with no actual issue.

## Real-World Example (Cycle #219)

Director pass found t_0c930c81 (exp_818) with dead process, 39m old. Initial diagnosis: "potentially stuck." But `kanban show --json` revealed `status=done` with a valid summary — the process exited after normal completion. Blocking would have been a false positive.

## Corrected Protocol

```
Process NOT found →
  1. Check `kanban show <id> --json` for actual status
  2. If status=done with summary → task is complete (zombie, not stuck). Do NOT block.
  3. If status=running with no completion events → truly crashed. Block with reason.
  4. If status=blocked → task was blocked by someone else. Do NOT touch.
```

## Detection Pattern

```python
import subprocess, json

def diagnose_dead_process(task_id):
    """Check if a dead-process task is actually done or truly stuck."""
    # Step 1: Confirm process is dead
    ps = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
    alive = any(task_id in line for line in ps.stdout.split('\n') if 'hermes' in line.lower())
    if alive:
        return "ALIVE — not stuck"
    
    # Step 2: Check actual kanban status
    subprocess.run(['hermes', 'kanban', 'show', task_id, '--json'], 
                   capture_output=True, text=True, timeout=10,
                   stdout=open('/tmp/kill_check.json', 'w'))
    d = json.load(open('/tmp/kill_check.json'))
    status = d.get('task', {}).get('status', '?')
    summary = d.get('latest_summary', '')
    
    if status == 'done' and summary:
        return f"DONE — process exited after completion. Summary: {summary[:100]}"
    elif status == 'running':
        return "STUCK — process dead, task still running. Block it."
    else:
        return f"OTHER — status={status}. Investigate."
```

## When to Apply

Every time `ps aux` shows no process for a running task, BEFORE blocking. The check takes <5 seconds and prevents false-positive blocks.

## Impact

Without this check, the Director blocks completed tasks, creating noise on the board and wasting operator time investigating non-issues.
