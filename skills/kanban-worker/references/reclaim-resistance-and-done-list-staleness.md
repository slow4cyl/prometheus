# Reclaim Resistance and Done List Staleness (Cycle #242)

## Reclaim Resistance

`kanban reclaim` releases the kanban claim, but the worker's hermes agent process may still be alive and continue occupying the worker slot. The task stays visible as `status=running` in `kanban list` despite successful reclaim.

### Detection

After reclaiming, re-check `kanban list --status running`. If the task still appears, verify with `kanban show <id> --json`. If status=running but the process is alive (`ps aux` confirms), the worker process hasn't exited yet.

### Fix Options

1. **Wait** — the process will eventually finish or crash.
2. **Kill the worker process**: `ps aux | grep <task_id>` to find PID, then `kill <PID>`.
3. **Force re-dispatch**: If the task's hypothesis is valid and you want it re-dispatched, block it first (`kanban_block`), then unblock — this forces a fresh worker spawn.

### Do NOT

- Repeatedly reclaim — each reclaim silently succeeds but doesn't change the outcome if the worker process is still alive.
- Assume reclaim "failed" — the CLI reports success, the issue is the worker process not exiting.

### Example (Cycle #242)

exp_1334 was reclaimed twice but stayed running because worker-2's hermes process hadn't exited. The task eventually completed on its own.

---

## Done List Staleness Mid-Pass

The done task list (`kanban list --status done --json`) is a point-in-time snapshot. New experiments can complete DURING a Director pass, making the list stale before unsynthesis detection runs.

### Impact

Unsynthesized experiment count is wrong — completed experiments are missed because they weren't in the done list when it was dumped.

### Fix

Re-dump the done list immediately before unsynthesis detection:

```bash
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done_fresh.json
```

Then use the fresh dump for the `done_exp_ids - ss_exp_ids` comparison.

### When This Matters

- Long Director passes where multiple experiments are running and likely to complete
- When the unsynthesis count is near the threshold (2-4) — a single missed completion changes whether synthesis is dispatched
- After creating/reclaiming tasks, which can trigger completions in other workers
