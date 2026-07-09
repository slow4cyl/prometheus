# Unassigned Queued Tasks — "Skipped (unassigned)" Pitfall

**Added:** Cycle #2857
**Category:** Dispatch assignment gap
**Distinct from:** `ready-task-assignment-deadlock.md` (tasks assigned to busy workers)

## Problem

`hermes kanban dispatch` reports `Skipped (unassigned): t_xxx, t_yyy, ...` for tasks that have NO `assignee` field set. The dispatcher cannot match these tasks to any worker, so they sit in the ready queue indefinitely — even when free workers exist.

## Observed Behavior (Cycle #2857)

```
hermes kanban dispatch
# Output:
# Spawned: 0
# Skipped (unassigned): t_88a8e1f0, t_380724bb, t_68e52819, ... (27 tasks)
# Deferred (prometheus-worker-4 at per-profile cap, 1 running): t_e1172ab3
```

27 tasks stuck in ready with no assignee. 5 workers free. Zero spawns.

## Root Cause

Synthesis workers or batch scripts create tasks without passing `--assignee`. The `kanban create` CLI defaults to no assignee when the flag is omitted. These tasks enter the ready queue but the dispatcher has no worker to match them to.

This is different from `ready-task-assignment-deadlock.md`:
- **Deadlock**: tasks assigned to workers that are now busy → reassign to free workers
- **Unassigned**: tasks have NO assignee at all → dispatcher skips them entirely

## Detection

```bash
# Count unassigned ready tasks
hermes kanban list --status ready --json 2>/dev/null | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
unassigned = [t for t in tasks if not t.get('assignee')]
print(f'Unassigned ready tasks: {len(unassigned)}')
for t in unassigned[:5]:
    print(f'  {t[\"id\"][:12]} | {t[\"title\"][:60]}')
"
```

## Fix

**Option A: Batch reassign unassigned tasks to free workers**
```bash
# Get free workers
FREE=$(ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u)
# ... compute free set ...

# Reassign unassigned tasks
python3 << 'PYEOF'
import json, subprocess
ready = json.loads(subprocess.run(
    ["hermes", "kanban", "list", "--status", "ready", "--json"],
    capture_output=True, text=True, timeout=15
).stdout)
unassigned = [t for t in ready if not t.get('assignee')]
free_workers = ["prometheus-worker-1", "prometheus-worker-16", ...]  # from ps aux
for i, task in enumerate(unassigned[:len(free_workers)]):
    worker = free_workers[i % len(free_workers)]
    subprocess.run(["hermes", "kanban", "assign", task['id'], worker], timeout=10)
    print(f"Assigned {task['id'][:12]} → {worker}")
PYEOF
```

**Option B: Delete unassigned tasks if they're stale/duplicate**
```bash
# For tasks that are clearly stale or duplicated by running experiments
hermes kanban archive <task_id>
```

**Option C: Let batch_create_tasks.py handle it**
The batch creator assigns tasks to specific workers. If unassigned tasks are from old synthesis cycles, they may be duplicates — the batch creator's coverage check will skip them for new, properly-assigned tasks.

## Prevention

1. **Batch creator always assigns**: `batch_create_tasks.py` sets `--assignee` on every task it creates. Use it instead of manual `kanban create`.
2. **Synthesis workers must assign**: When synthesis creates follow-up experiment tasks, it MUST set `--assignee prometheus-worker-N`.
3. **Director checks before dispatch**: Before calling `hermes kanban dispatch`, count unassigned ready tasks. If > 0, either reassign them or archive stale ones.

## When This Matters Most

- After synthesis cycles that create many follow-up tasks without assignees
- When the Director uses `kanban create` manually instead of `batch_create_tasks.py`
- In long-running systems where stale unassigned tasks accumulate over many cycles

## Integration with Director Flowchart

Added as Step 5c in the Director Quick-Pass Flowchart (between 5b topic duplicate check and 6 task creation). This is the correct position because:
1. Assigning BEFORE batch_create_tasks.py prevents per-profile cap conflicts
2. Free workers get assigned existing tasks before new ones are created
3. Dispatcher can immediately spawn assigned tasks on next dispatch call

The step queries for unassigned ready tasks, assigns them to free workers, then dispatches. If no free workers remain after assignment, skip to synthesis (step 3) or curation.
