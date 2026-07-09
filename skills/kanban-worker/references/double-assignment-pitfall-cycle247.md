# Double-Assignment After batch_create (Cycle #247)

## Problem

When using `batch_create_tasks.py` to assign tasks to free workers, creating additional tasks assigned to those same workers in the same Director pass causes double-assignment.

## Reproduction

1. Batch creator assigns 4 tasks to free workers: workers 5, 7, 10, 22
2. Director then creates a curation task also assigned to worker-5
3. Worker-5 now has 2 tasks queued — experiment + curation
4. Second task queues behind the first, wasting a dispatch slot

## Detection

```bash
# Check for workers running 2+ hermes processes
ps aux | grep 'hermes' | grep -v grep | grep 'kanban task' | \
  grep -oE 'prometheus-worker-[0-9]+' | sort | uniq -c | sort -rn | head
```

Workers with count > 1 have double-assignments.

## Fix

After batch_create, parse its output to track assigned workers:

```
Created: t_edc8eafb → prometheus-worker-5 [transfer] score=89
Created: t_2c55646b → prometheus-worker-7 [ensemble] score=80
```

Subtract {5, 7, 10, 22} from the free set before creating manual tasks. Only assign additional tasks to workers NOT claimed by batch_create.

## Prevention

The batch_create output lists assigned workers explicitly. Parse it:

```python
import re
batch_output = "Created: t_xxx → prometheus-worker-5 ..."
assigned_workers = set(int(m) for m in re.findall(r'prometheus-worker-(\d+)', batch_output))
free_after_batch = full_set - assigned_workers
# Only create manual tasks for free_after_batch
```

## Impact

- Worker processes 2 tasks sequentially instead of in parallel
- Dispatch slot wasted on queued task
- No data corruption, just efficiency loss
