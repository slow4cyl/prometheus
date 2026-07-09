# Block Over Reclaim for Topic Duplicates

**Added: Cycle #245 (June 2026)**
**Confirmed by: Director pass cycle where 3 reclaimed duplicates were immediately re-dispatched**

## The Problem

When the Director detects topic duplicates among running tasks, the instinct is to `reclaim` them. This is wrong — reclaiming causes immediate re-dispatch, creating a reclaim→re-dispatch loop that wastes time and fills per-profile caps.

## What Happens

```
Director detects 3 duplicates → reclaims them
    ↓
Scheduler immediately re-dispatches reclaimed tasks to same workers
    ↓
Re-dispatched tasks fill per-profile caps (1 task per worker)
    ↓
Newly created tasks are ALL deferred (per-profile cap full)
    ↓
Net result: 0 capacity freed, time wasted
```

## The Correct Approach

**BLOCK duplicates instead of reclaiming.** Blocking removes the task from dispatch entirely — it won't be re-dispatched.

```
Director detects 3 duplicates → blocks them with reason "Duplicate topic — older instance running"
    ↓
Blocked tasks removed from dispatch queue
    ↓
Worker processes finish naturally (or can be killed if needed)
    ↓
Per-profile cap frees up → new tasks can be dispatched
```

## Code Pattern

```python
# WRONG — reclaiming
hermes kanban reclaim "$task_id" --reason "duplicate topic"

# RIGHT — blocking
hermes kanban block "$task_id" "Duplicate topic — older instance already running. Will be resolved by the older experiment."
```

## When Reclaiming IS Appropriate

- Worker process is dead (check `ps aux` — no matching process)
- Task is truly stuck (>60min, no heartbeat, worker not in process list)
- Operator explicitly wants to re-assign to a different worker

## When Blocking IS Appropriate

- Topic duplicate (same experiment running twice)
- Stale task that will be superseded by a newer instance
- Any case where you want the task removed from dispatch but NOT re-dispatched

## Detection After Block

After blocking duplicates, verify the state:
```bash
hermes kanban list --status running --json | python3.11 -c "
import json, sys
tasks = json.load(sys.stdin)
print(f'Running: {len(tasks)}')
"
```

If running count dropped, the blocks worked. Then dispatch to fill freed capacity:
```bash
hermes kanban dispatch
```

## Related Pitfalls

- `reclaim-then-re-dispatch cycle` (this skill's pitfall section)
- `batch creator deferral when re-dispatched tasks fill per-profile cap`
- `topic duplicates slip through diversity cap`
