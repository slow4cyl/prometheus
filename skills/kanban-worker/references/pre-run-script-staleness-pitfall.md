# Pre-Run Script Data Staleness — Cycle 246

## The Problem

The Director loop pre-run script injects context from self_state.json into the Director's prompt. This data can be stale — self_state.json is only updated by the synthesis worker, which runs periodically. Between synthesis cycles, the pre-run data may not reflect the current board state.

## Real Example (Cycle 246, 2026-06-02)

The pre-run script reported:
```
No running experiments.
Last 3 completed experiments: [exp_1489, exp_1494, exp_1495]
```

But the live kanban board showed:
- 12 running tasks (11 experiments + 1 synthesis)
- 11 active worker profiles

If the Director had trusted the pre-run data, it would have:
1. Incorrectly concluded all workers were free
2. Created duplicate experiment tasks for items already being investigated
3. Wasted worker slots and API budget

## The Fix

**Always verify operational data against the live board:**
- Worker count: `ps aux | grep prometheus-worker | grep -v grep`
- Running tasks: `hermes kanban list --status running --json`
- Task status: `hermes kanban show <id> --json`

**Pre-run data is useful for:**
- Queue size and composition (curiosity_queue doesn't change rapidly)
- Last completed experiments (for context on what's been done)
- Knowledge gaps and self-state version

**Pre-run data is NOT reliable for:**
- Running task count (tasks start/complete between synthesis cycles)
- Worker availability (workers spawn/die dynamically)
- Whether a specific experiment is currently running
