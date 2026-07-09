# Worker Duplication Pitfall (added 2026-06-01)

## Problem

The `ps aux | grep 'prometheus-worker' | sort -u` pattern deduplicates worker profiles, so a worker running 2 tasks appears only once in the busy set. The free worker count is correct, but the Director doesn't detect that the same worker was double-assigned.

## Real-World Example

In the 2026-06-01 Director pass, worker-6 had two concurrent tasks:
- `t_2aff832f` (exp_575: Cross-domain immunity)
- `t_a4643545` (exp_539: MUTUAL_BENEFIT directionality)

Both were running, both actively producing output. The `sort -u` dedup showed worker-6 once, so the free worker count was correct (8 free), but the duplication was invisible.

## Detection

After extracting busy workers from `ps aux`, count occurrences of each profile:

```bash
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort | uniq -c | sort -rn
```

Any profile appearing 2+ times is double-assigned.

## Fix (mid-pass)

If duplication is detected during a Director pass:
1. Identify the newer task (higher task age = newer dispatch)
2. Reclaim it: `hermes kanban reclaim <task_id>`
3. Block it: `hermes kanban block <task_id> "duplicate assignment to worker-N"`

## Prevention

Maintain a `set()` of assigned workers during task creation:

```python
assigned_workers = set()
for task in tasks:
    if task['assignee'] in assigned_workers:
        print(f"SKIP: {task['assignee']} already assigned")
        continue
    # create task
    assigned_workers.add(task['assignee'])
```

The free worker calculation (`full_set - busy_set`) tells you WHICH workers are available; the assignment tracker tells you WHICH you've already used in this pass.

## Why It Happens

The dispatcher may re-dispatch a task to the same worker if:
1. The worker's previous task completed but the process hasn't fully exited (zombie)
2. The dispatcher's tick fires before the previous dispatch is fully cleaned up
3. Two dispatch cycles overlap (rare but possible with fast cron ticks)
