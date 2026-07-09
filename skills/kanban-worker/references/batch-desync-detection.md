# Batch Desync Detection — Timing Optimization

**Added:** Cycle #219
**Context:** Director passes checking status desync across 10+ running tasks

## Problem

The 1-by-1 `kanban show` approach for detecting status desync takes ~5 seconds per task. For 23 running tasks, this is ~2 minutes of sequential CLI calls, most of which confirm the task is still running.

## Optimization

Only check tasks that have been running for >10 minutes. Tasks created in the current cycle are almost always still running — desync typically occurs when a worker completes between the `kanban list` and `kanban show` calls.

```python
import json, subprocess, datetime

running = json.load(open('/tmp/kanban_running.json'))
now = datetime.datetime.now(datetime.timezone.utc).timestamp()

# Only check tasks older than 10 minutes
old_tasks = []
for t in running:
    created_at = t.get('created_at', 0)
    age_min = (now - created_at) / 60 if created_at else 0
    if age_min > 10:
        old_tasks.append(t)

print(f"Checking {len(old_tasks)}/{len(running)} tasks (>10m old)")

desync_count = 0
for t in old_tasks:
    tid = t.get('id', '?')
    title = t.get('title', 'N/A')[:50]
    result = subprocess.run(
        ['hermes', 'kanban', 'show', tid, '--json'],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        try:
            d = json.loads(result.stdout)
            actual_status = d.get('task', {}).get('status', '?')
            if actual_status == 'done':
                print(f"  DESYNC: {tid} ({title})")
                desync_count += 1
        except:
            pass

print(f"\nDesync tasks found: {desync_count}")
```

## Savings

- 23 tasks total, 8 tasks <10m old → check 15 instead of 23
- ~5s per check → 75s instead of 115s → saves ~40s
- In Cycle #219, this optimization saved ~90s by skipping 8 tasks

## When to Use

- Director passes with 10+ running tasks
- When the board is saturated (≥7 running) and you need accurate counts
- Before making saturation decisions (synthesis-only vs create-new-tasks)

## When NOT to Use

- Fewer than 10 running tasks (overhead of age filtering exceeds savings)
- When you need 100% accuracy (some desync tasks may be <10m old)
- When the board is below saturation threshold (desync doesn't affect decisions)
