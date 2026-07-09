# Bulk Status Desync Detection (added Cycle #602)

## Problem

Spot-checking 2-3 tasks from `kanban list --status running` misses desync. In Cycle #602:
- Listed: 13 running tasks
- Spot-checked 2/13: found 2 desyncs
- Checked ALL 13: found 4 desyncs
- True running count: 9 (not 13)
- Free workers: 12 (not 8 as naive count suggested)

This changes Director decisions: 12 free workers justifies creating experiment tasks; 8 is borderline.

## Pattern

After dumping `kanban list --status running --json`, verify EVERY task with `kanban show <id> --json` in a single Python pass:

```python
import json, subprocess
running = json.load(open('/tmp/kanban_running.json'))
actual_running = 0
desync_done = 0
for t in running:
    tid = t['id']
    r = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'],
                      capture_output=True, text=True, timeout=10)
    d = json.loads(r.stdout)
    actual = d.get('task', {}).get('status', '?')
    if actual == 'running':
        actual_running += 1
    else:
        desync_done += 1
        print(f'DESYNC: {tid} listed=running actual={actual}')
print(f'Actual running: {actual_running}')
print(f'Desync (done but listed running): {desync_done}')
```

## Corrected Free Worker Calculation

```
busy_from_ps_aux = count of unique prometheus-worker-N in ps aux
true_busy = busy_from_ps_aux - desync_done
true_free = 22 - true_busy
```

In Cycle #602: `true_free = 22 - (14 - 4) = 12` (not `22 - 14 = 8`).

## When to Run

At the START of every Director pass, before the "CHECK WORKER AVAILABILITY" step. The 13-task loop takes ~15 seconds — negligible overhead vs the risk of misallocating workers.

## Impact on Saturation Decision

The saturation threshold is 7 running tasks. With desync, the Director may see 13 (above threshold → synthesis-only) when true count is 9 (still above, but closer). More critically, the free worker count changes whether the "free_workers ≥ 3 AND 3+ open queue items" exception applies.
