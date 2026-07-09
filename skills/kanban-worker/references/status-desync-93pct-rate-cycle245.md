# 93% Status Desync Rate + Two-Pass Verification (Cycle #245, 2026-06-02)

## Context
Routine Director pass with 15 tasks in `kanban list --status running`. After zombie cleanup and individual `kanban show --json` verification, 14 of 15 tasks (93%) were actually done. This is the highest desync rate observed.

## The Problem
The `kanban list --status running` endpoint returns tasks that have already completed via `kanban_complete` — the list view doesn't update status in real-time. Previous documentation covered individual desync cases (Cycle #145, #161, #180), but this session revealed the desync can affect nearly ALL running tasks simultaneously.

## Two-Pass Verification Pattern

### Pass 1: Zombie Detection + Cleanup
```bash
# Count workers vs running tasks
WORKERS=$(ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u | wc -l)
RUNNING=$(cat /tmp/kanban_running.json | python3.11 -c "import json,sys; print(len(json.load(sys.stdin)))")

# If worker count > running task count, there are zombies
# Cross-reference ps aux against kanban list to identify which PIDs to kill
```

### Pass 2: Status Desync Re-verification (MANDATORY)
After killing zombies, spot-check 3-5 "running" tasks with `kanban show <id> --json`:
```python
import json, subprocess
task_ids = [...]  # from kanban list
for tid in task_ids[:5]:  # spot-check first 5
    r = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'], capture_output=True, text=True, timeout=10)
    d = json.loads(r.stdout)
    status = d.get('task', {}).get('status', '?')
    if status != 'running':
        print(f"DESYNC: {tid} shows 'running' in list but is actually '{status}'")
```

**Key insight:** Even after zombie cleanup, the remaining "running" tasks may include more desynced-done tasks. The list is unreliable — always verify with `kanban show` before making saturation decisions.

## Blocked→Ready Desync
One task (t_36484f11) showed `status=blocked` in one `kanban show` check but `status=ready` in a subsequent check. This suggests the status can fluctuate between checks, possibly due to concurrent operations or dispatcher state changes.

**Lesson:** Don't act on a single status check. If a task shows blocked/ready and you're unsure, recheck after a few seconds.

## Impact on Director Decisions
- **Saturation count:** Using list count (15) would suggest saturation (≥7 threshold). Verified count (9) is below threshold — allows task creation.
- **Free worker count:** List suggests 7 free (22-15). Verified suggests 13 free (22-9). Massive difference in task creation capacity.
- **Task creation:** Creating tasks for desynced-done tasks wastes worker slots. Always cross-reference before creating.

## Recommendation
The Director Quick-Pass Flowchart step 2 should be updated to include mandatory status desync re-verification as a sub-step, not just a conditional "if worker count > running task count" check. The 93% desync rate means the list is essentially unreliable for saturation decisions.
