# Director: Zero-Candidate Queue During Saturation (Cycle #201)

## Problem

When queue triage yields zero independent candidates (no NO_SOURCE items with LOW coverage, no DONE/ACTIVE items whose question is genuinely uncovered), the Director may be tempted to create tasks "just because workers are free." This wastes workers on already-covered topics.

## Observed in Cycle #201

- 16 running tasks, 6 free workers
- 45 queue items, ALL classified DONE/UNKNOWN/RUNNING
- Zero NO_SOURCE items with LOW coverage
- Zero DONE/ACTIVE items with genuinely uncovered questions
- Result: skip task creation entirely, just ensure synthesis exists and dispatch

## Decision Flow

```
1. Run queue classification (RESOLVED/RUNNING/DONE/NO_SOURCE/UNKNOWN)
2. Filter: keep only NO_SOURCE (with LOW coverage) and DONE/ACTIVE (question not answered)
3. If result set is EMPTY:
   a. Verify all running tasks are healthy (ps aux + workspace check)
   b. Ensure synthesis task exists for unsynthesized experiments
   c. Log the pass
   d. Dispatch (scheduler picks up any ready tasks)
   e. Do NOT create tasks "because workers are free"
4. If result set is NON-EMPTY:
   a. Create tasks for independent items only
   b. Fill free workers, respect saturation threshold
```

## Key Insight

The queue triage is the gate for task creation, not worker count. Having free workers during saturation with a well-covered queue is normal — those workers will be utilized naturally when current tasks complete and new ready tasks are dispatched.

## Anti-Pattern

Creating tasks for DONE items whose source experiment already answered the question. The classification marks them DONE but the Director must verify the question is actually answered before creating a follow-up. See "DONE category conflates answered vs touched topic" pitfall.
