# False Positive: "Zero Heartbeats, Age=999m" Stuck Detection (Cycle #251)

## Problem

The stuck task heuristic checks `kanban show --json` events for heartbeats. When 0 heartbeats are found, the code computes age as 999m (sentinel value). This triggers on ALL Python script workers (which don't send heartbeats) AND recently-dispatched hermes agents.

In Cycle #251, ALL 18 running tasks showed "stuck: 0 heartbeats, age=999m" — but 16 were healthy Python scripts actively writing output files.

## Why It Happens

- Heartbeats are a **hermes-agent feature**, not a Python feature
- Python experiment scripts run via `terminal(background=true)` don't send heartbeats
- The 999m sentinel is a default when no heartbeat events are found in the events array
- `kanban list --json` never returns events (always 0), making list-based checks unreliable

## Detection Pattern

After the 999m sentinel triggers, cross-reference with:

1. **Process liveness**: `ps aux | grep <task_id>` — if process exists, worker is alive
2. **Workspace file activity**: Check if workspace has recent output files (< 15 min old)
3. **CPU time**: `ps -p <pid> -o time` — healthy workers show cumulative CPU > 0

## Decision Matrix

| Process | Workspace Activity | Diagnosis |
|---------|-------------------|-----------|
| Alive | Recent files (<15m) | HEALTHY — leave alone |
| Alive | Only original script, 60+ min | HUNG — reclaim + block |
| Alive | Empty, <10 min old | JUST DISPATCHED — wait |
| Dead | Any | CRASHED — block immediately |

## Fix

The stuck detection code should skip tasks where:
- `ps aux` confirms a live process AND
- Workspace mtime < 15 minutes

The 999m sentinel is unreliable as a sole signal — it must be combined with process and workspace checks.

## Related

- See "Stuck Worker Protocol" in kanban-worker SKILL.md
- See "Method 3: Workspace file modification time" in kanban-worker SKILL.md
- See "Alive but silent" hung process pattern
