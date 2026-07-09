# Director Duplicate Task Judgment (added Cycle #293)

## When Duplicate Topics Are Complementary (Keep Both)

Not all topic-duplicate tasks should be reclaimed. Two tasks investigating the same question from different methodological angles may produce complementary results that synthesis can merge.

### Decision Matrix

| Signal | Keep Both | Reclaim Newer |
|--------|-----------|---------------|
| Different methods | ✅ (e.g., focused Q38 test vs broad 10-example survey) | |
| Different scope | ✅ (e.g., single domain vs cross-domain) | |
| Different models | ✅ (e.g., mimo-v2.5 vs DeepSeek comparison) | |
| Same method, same scope | | ✅ (truly redundant) |
| One task is >30min older | ✅ (older has more invested) | |
| Both just dispatched (<5min) | | ✅ (no sunk cost) |

### Examples from Cycle #293

**Complementary (kept both):**
- exp_565 (46m): "Asyncio erosion resistance — Q38 specifically" 
- exp_577 (3m): "Asyncio erosion resistance — 10 examples vs sync control"
  → Different scope: one focused, one broad. Synthesis can merge.

- exp_569 (33m): "Anti-manipulation framing — 4 conditions × 3 domains"
- exp_578 (3m): "Anti-manipulation framing — mechanism decomposition"
  → Different method: one measures, one explains mechanism.

**Truly redundant (would reclaim):**
- Two tasks with identical method, scope, and models — one will produce identical results.

### Rule of Thumb

When in doubt, keep both. The synthesis worker can merge results from complementary tasks. Reclaiming a complementary task wastes the API calls already made and loses potentially valuable methodological diversity.

## Worker Double-Assignment Detection

### Problem

Director may assign 2 tasks to the same worker if it doesn't check worker availability before creating tasks. This means some tasks are assigned but not running (the worker can only run one at a time).

### Detection Pattern

```python
import json
d = json.load(open('/tmp/kanban_running.json'))
workers = {}
for t in d:
    a = t.get('assignee', 'none')
    if a not in workers:
        workers[a] = []
    workers[a].append(t['id'])
for w, tasks in sorted(workers.items()):
    if len(tasks) > 1:
        print(f'DOUBLE-ASSIGNED: {w}: {len(tasks)} tasks -> {tasks}')
```

### Impact

- 23 tasks with 20 workers where 3 workers are double-assigned = effective capacity is 20, not 23
- Both tasks may have LIVE processes — the dispatcher CAN spawn multiple hermes agent processes for the same worker profile. Each process runs independently. This wastes API budget on duplicate work.
- The "running" count overstates actual parallel execution AND hides wasted resources

### Detection (quick)

```python
from collections import Counter
workers = [t.get('assignee','?') for t in d]
dupes = {w: c for w, c in Counter(workers).items() if c > 1}
```

### Prevention

Always run the worker availability check (`ps aux | grep prometheus-worker`) BEFORE creating tasks. Map each running task to its worker. Only assign to workers with 0 running tasks.

### Fix (full cleanup workflow)

When double-assignment is detected:

1. **Identify which task to reclaim**: Reclaim the OLDER task (more time invested is usually wasted if the newer task covers the same topic). If topics are different, reclaim whichever is lower priority.

2. **Verify both processes are live**:
   ```bash
   for tid in <task_ids>; do
       count=$(ps aux | grep "$tid" | grep -v grep | wc -l)
       echo "$tid: $count processes"
   done
   ```

3. **Reclaim the stale duplicate**:
   ```bash
   hermes kanban reclaim <stale_task_id>
   ```

4. **Verify board is clean** (no more double-assignments):
   ```bash
   hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json
   # Then re-run the Counter check
   ```

5. **Optionally re-dispatch** the reclaimed task to a free worker if the hypothesis is still valuable.

**Real-world example (this session):** 3 workers double-assigned — worker-1 (exp_695 + exp_742), worker-7 (exp_718 + exp_751), worker-8 (exp_719 + exp_732). All 6 tasks had live processes. Reclaimed the 3 older tasks (exp_695, exp_718, exp_719) which were duplicates of newer tasks covering the same topics. Board went from 19 → 16 running, 0 double-assignments.
