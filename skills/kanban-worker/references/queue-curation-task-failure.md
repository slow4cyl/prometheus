# Queue Curation Task Failure Pattern

**Observed:** June 3, 2026 (Director pass)

## Symptom

Queue curation task `t_36e30cc8` assigned to `prometheus-worker-5`:
- Completed in 4 minutes with status=done
- No workspace created (`~/.hermes/kanban/workspaces/t_36e30cc8/` did not exist)
- No result field set
- No comments posted
- Queue remained at 61 items (unchanged)

## Root Cause

The worker likely either:
1. Could not access `self_state.json` (approval system blocking writes)
2. Didn't understand the task body instructions
3. Completed the task without actually performing the curation

This is similar to the "synthesis worker rogue behavior" pattern — the worker marks the task as done without doing the work.

## Detection

After a curation task completes:
```bash
# Check queue size
python3.11 -c "
import json
state = json.load(open('~/.hermes/self_state.json'))
queue = state.get('curiosity_queue', [])
print(f'Queue: {len(queue)} items')
"

# Check if workspace existed
ls -la ~/.hermes/kanban/workspaces/<task_id>/ 2>/dev/null || echo "No workspace"

# Check task result
hermes kanban show <task_id> --json 2>/dev/null | python3.11 -c "
import json, sys
task = json.load(sys.stdin)
t = task.get('task', task)
print(f'Result: {t.get(\"result\", \"none\")}')
"
```

## Fix

If queue is still >50 after curation task completes:
```bash
python3 ~/.hermes/scripts/queue_cleanup.py --apply
```

## Prevention

1. **Director must verify queue size after curation** — don't assume the task worked
2. **Curation tasks are best-effort** — treat them as hints, not guarantees
3. **Run `queue_cleanup.py --apply` directly when queue >50** — it's more reliable than delegating to a worker

## Related Pitfalls

- "Synthesis worker rogue behavior" — similar pattern of task completion without work
- "Approval system blocks synthesis worker writes" — workers can't modify self_state.json
