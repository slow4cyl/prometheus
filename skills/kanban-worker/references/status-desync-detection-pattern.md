# Status Desync Detection Pattern

## Problem

`hermes kanban list --status running --json` can include tasks that are actually `status=done`. This inflates the running count and can push the Director above the saturation threshold when the true count is lower.

## Detection Pattern

### Step 1: Get running tasks from list

```bash
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json
```

### Step 2: Verify each task's actual status

```python
import json, subprocess

running = json.load(open('/tmp/kanban_running.json'))
stuck = []
active = []
potentially_done = []

for t in running:
    tid = t['id']
    result = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'], 
                          capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        d = json.loads(result.stdout)
        status = d.get('task', {}).get('status', '?')
        
        if status == 'done':
            potentially_done.append(tid)
        elif status == 'running':
            # Check if process is alive
            ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
            process_alive = tid in ps_result.stdout
            
            if process_alive:
                active.append(tid)
            else:
                stuck.append(tid)

print(f'Done (desync): {len(potentially_done)}')
print(f'Running (active): {len(active)}')
print(f'Running (stuck): {len(stuck)}')
```

### Step 3: Use verified count for saturation decision

```python
verified_running = len(active)
if verified_running >= 7:
    # SYNTHESIZE-ONLY mode
else:
    # Create tasks for free workers
```

## Observed Behavior (Cycle 278)

- List showed 19 running tasks
- Verification revealed:
  - 9 tasks with status=done (desync)
  - 10 tasks actually running with live processes
  - 0 stuck tasks
- True running count: 10 (below saturation threshold of 7, but close)

## Impact

Without verification, the Director would have:
- Applied synthesis-only mode (19 > 7 threshold)
- Missed the opportunity to create 9 new experiment tasks
- Wasted 12 free worker slots

## When to Use

- When running count is near the saturation threshold (5-9 tasks)
- When worker count doesn't match running task count
- At the start of every Director pass for accurate board state

## Alternative: Process Count Method

For faster bulk checking, count active worker processes:

```bash
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u | wc -l
```

This gives the true number of active workers, which should match the verified running task count. If worker count < running task count, the difference are desynced tasks.
