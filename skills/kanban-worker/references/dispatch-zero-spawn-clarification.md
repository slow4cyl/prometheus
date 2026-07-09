# Dispatch "Spawned: 0" — Normal Behavior, Not Failure

**Added:** Cycle #252
**Category:** Dispatch spillover clarification

## Problem

When `hermes kanban dispatch` reports "Spawned: 0" after creating tasks, it looks like a failure. Directors may waste time diagnosing a non-problem, re-running dispatch, or creating duplicate tasks.

## Root Cause

The dispatch command triggers the scheduler, but the scheduler processes the ready queue asynchronously. Tasks are created in `ready` status, and the scheduler picks them up on its next tick (every ~1 minute). The "Spawned: 0" output means the scheduler hasn't processed the queue yet — not that tasks were rejected.

## Observed Behavior (Cycle #252)

```
# Created 4 experiment tasks + dispatched
hermes kanban dispatch
# Output: Spawned: 0

# Waited ~30 seconds, checked status
hermes kanban list --status running
# Output: 8 running tasks (5 original + 4 new - 1 completed)
# All 4 new tasks were running — scheduler picked them up
```

The dispatch returned 0 because the scheduler tick hadn't fired yet. All tasks moved to running within 30-60 seconds.

## What to Do

1. **Do NOT re-run dispatch** — it won't help and may cause confusion
2. **Wait 1-2 minutes** — the scheduler will process the queue on its next tick
3. **Verify with `hermes kanban list --status running --json`** — count should increase
4. **If tasks are still in `ready` after 2+ minutes**, then there's a real issue (worker capacity, profile misconfiguration, etc.)

## Gateway Dispatch vs CLI Dispatch (June 2026)

When `dispatch_in_gateway: true` is set in config, the gateway runs its own
dispatch loop every `dispatch_interval_seconds` (default 60s). The CLI
`hermes kanban dispatch` command is REDUNDANT when the gateway is active —
the gateway handles spawning automatically.

**Observed behavior:** CLI dispatch returns `Spawned: 0` repeatedly, but
tasks are being spawned by the gateway. Running count increases despite
CLI showing 0 spawned.

**Verification:** Check if gateway is dispatching:
```bash
ps aux | grep "gateway run" | grep -v grep
```

If gateway is running, trust the gateway dispatch. The CLI command is
only needed when the gateway is stopped or for manual intervention.

## When "Spawned: 0" IS a Problem

- Tasks stay in `ready` for 5+ minutes with workers available
- `hermes kanban list --status ready --json` shows tasks but no workers are spawning
- Worker profiles are misconfigured or missing from the dispatch config
- Task body is NULL (see `bodyless-task-dispatch-skip.md`)

## Integration with Existing Pitfalls

This supplements the "Dispatch spillover" pitfall in the main SKILL.md. That pitfall documents that not all tasks spawn on the first call; this clarifies that "Spawned: 0" is a valid subset of that behavior.
