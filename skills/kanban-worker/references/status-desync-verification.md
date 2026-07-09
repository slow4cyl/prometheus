# Status Desync Verification Pattern

Added Cycle #270. The `kanban list --status running` output includes tasks that are actually `done` — the list endpoint doesn't update status in real-time after `kanban_complete`.

## Problem

In Cycle #270, the Director counted 10 "running" tasks but only 5 were truly running. The other 5 had completed (status=done in `kanban show --json`) but still appeared in the running list. This inflated the saturation count, nearly causing the Director to skip task creation.

## Detection Pattern

```python
import json, subprocess

running = json.load(open('/tmp/kanban_running.json'))
truly_running = []
desynced = []

for t in running:
    tid = t['id']
    r = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'], 
                      capture_output=True, text=True, timeout=10)
    d = json.loads(r.stdout)
    actual_status = d.get('task', {}).get('status', '?')
    
    if actual_status == 'done':
        desynced.append(t)
    else:
        truly_running.append(t)

print(f"List: {len(running)}, Truly running: {len(truly_running)}, Desynced: {len(desynced)}")
```

## When to Run

**Before counting "truly running" tasks** in the Director flowchart (step 1b). This is a prerequisite for:
- Saturation threshold check (≥7 running → synthesis-only mode)
- Free worker calculation
- Queue classification (which source experiments are still running)

## Impact

- In Cycle #270: 5/10 tasks were desynced. True count was 5 (below saturation of 7), enabling task creation that would have been skipped.
- In Cycle #185: 1/7 tasks was desynced. True count was 6 (below threshold), enabling task creation.

## Optimization

For 10+ running tasks, batch the `kanban show` calls to avoid sequential API overhead:

```bash
for tid in $(cat /tmp/kanban_running.json | python3 -c "import json,sys; [print(t['id']) for t in json.load(sys.stdin)]"); do
    hermes kanban show "$tid" --json > "/tmp/check_${tid}.json" 2>&1 &
done
wait
```

Then process all files in a single Python command.
