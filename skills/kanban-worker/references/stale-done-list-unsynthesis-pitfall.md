# Stale Done List — Missed Unsynthesized Experiments

**Added:** June 2026 (Director pass cron job)

## Problem

The `kanban list --status done --json` output can become stale between calls. When a Director pass dumps the done list early (e.g., at step 1 of the flowchart) and then runs unsynthesis detection later, newly-completed tasks are missing from the dump.

## Reproduction

1. Director pass starts, dumps `kanban list --status done --json` → 1984 tasks
2. Pass runs queue triage, checks worker availability, creates experiment tasks
3. Meanwhile, 2 tasks complete (exp_1645, exp_1650)
4. Unsynthesis detection runs against stale list → only 2 unsynthesized found
5. Fresh query reveals 4 unsynthesized (exp_1645, exp_1650, exp_1649, exp_1652)
6. Missed experiments = lost synthesis, counter drift

## Fix

Always re-run `hermes kanban list --status done --json` **immediately before** the unsynthesis comparison. Do NOT reuse a stale dump from earlier in the pass.

```bash
# WRONG — done list from step 1, stale by step 3
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json  # step 1
# ... 5 minutes of queue triage, task creation ...
# unsynthesis detection uses /tmp/kanban_done.json — MISSES new completions

# RIGHT — refresh before comparison
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json  # step 1 (for context)
# ... queue triage, task creation ...
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done_fresh.json  # refresh
# unsynthesis detection uses /tmp/kanban_done_fresh.json
```

## Impact

The done list query is cheap (~2 seconds). The cost of missing unsynthesized experiments is high: lost synthesis, untracked experiments, counter drift between `experiments.completed` array and metrics counters.

## Integration with Director Flowchart

Insert a "REFRESH DONE LIST" step between step 3 (DETECT UNSYNTHESIZED) and step 4 (CLASSIFY QUEUE ITEMS) in the Director Quick-Pass Flowchart:

```
3. DETECT UNSYNTHESIZED
   hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json  # FRESH
   ... comparison logic ...

3b. REFRESH DONE LIST (if pass has been running > 30 seconds)
   hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json
```
