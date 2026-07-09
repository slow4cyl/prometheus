# Archive as Escape Hatch for Block-Reclaim-Dispatch Loops

## Problem

When a task is blocked (e.g., for being redundant), the Janitor or Director may reclaim it to free the worker slot. But `reclaim` puts the task back into `ready` status, and the dispatcher re-dispatches it on the next tick. A new worker spawns, discovers the same problem, and blocks it again. This creates an infinite loop:

```
blocked → reclaimed → ready → dispatched → blocked → reclaimed → ...
```

Each cycle wastes a worker spawn, an API call, and a block event. The task accumulates runs (each producing the same "redundant" block) while consuming resources.

## Real-World Example (Cycle #279)

Task `t_e189c05a` was blocked **14 times** across 14 dispatch cycles. Each time:
1. Worker spawned, ran `kanban_show`, discovered the task was redundant of exp_1411
2. Worker called `kanban_block(reason="Redundant: Nth dispatch...")`
3. Task went back to `ready`
4. Dispatcher spawned a new worker on next tick
5. Repeat

Total waste: 14 worker spawns, 14 API calls, 14 block events — all for a task that was known-redundant after the 2nd block.

## Solution: `hermes kanban archive <task_id>`

The `archive` command permanently removes a task from the dispatch queue. It:
- Changes status to `archived`
- Prevents all future re-dispatch
- Preserves the full event history (runs, comments, blocks) for audit
- Does NOT delete the task from the database

## When to Archive vs Block

| Situation | Action | Why |
|-----------|--------|-----|
| Task blocked 1-2 times, may need re-investigation | Block | Preserve option to retry with different approach |
| Task blocked 3+ times for same reason | **Archive** | Pattern is clear — re-dispatch will produce same result |
| Duplicate of another task | **Archive** (or block if newer) | Permanently prevent re-dispatch |
| Worker keeps crashing on same task | Block first, then archive if root cause is in task spec | Block gives operator visibility; archive prevents waste |
| Task hypothesis definitively refuted | **Archive** | No value in re-running |

## Detection Pattern

At the start of each Director pass, after reading the board state, check for tasks with 3+ block events in their history:

```python
# After loading running/done task lists
import subprocess, json

# For each task in running or ready status
for task_id in suspect_task_ids:
    result = subprocess.run(
        ['hermes', 'kanban', 'show', task_id, '--json'],
        capture_output=True, text=True, timeout=10
    )
    d = json.loads(result.stdout)
    events = d.get('task', {}).get('events', []) or []
    block_count = sum(1 for e in events if e.get('kind') == 'block')
    if block_count >= 3:
        print(f"ARCHIVE CANDIDATE: {task_id} — blocked {block_count} times")
        subprocess.run(['hermes', 'kanban', 'archive', task_id])
```

## Archive vs Reclaim

- **Reclaim**: Releases the worker claim, puts task back to `ready`. Use when you want re-dispatch (e.g., transient API failure, wrong worker assignment).
- **Archive**: Permanently removes from dispatch. Use when the task itself is the problem (redundant, refuted, or spec is broken).

Reclaim is for "try again with a different worker." Archive is for "stop trying."
