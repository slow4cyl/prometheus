# Director Pass Checklist — Cycle #225

Quick reference for the reclaim-then-block pattern discovered during this cycle.

## What Happened

Two tasks (exp_891, exp_893) had complete results.json files in their workspaces but never called `kanban_complete()`. Both showed:
- Status: `running`
- Heartbeats: 0
- Completions: 0
- Process: alive (zombie)
- Workspace: has results.json + output.log with "Completed:" marker

## What We Did

1. Reclaimed both tasks
2. exp_891 resolved naturally (status flipped to done after reclaim)
3. exp_893 was immediately re-dispatched by the dispatcher
4. Had to block exp_893 to prevent wasted re-run

## Lesson

**Always block after reclaim for results-complete tasks.** The dispatcher treats reclaimed tasks as `ready` and re-dispatches them. For tasks where the work is already done, this wastes a worker slot.

## Correct Pattern

```bash
# Step 1: Reclaim
hermes kanban reclaim <task_id>

# Step 2: Block immediately (prevents re-dispatch)
hermes kanban block <task_id> "results complete from prior run — <filename> exists"
```

## When Reclaim-Only Is OK

- Transient API failure (hypothesis sound, just need fresh worker)
- Task body incomplete (original task was poorly specified)
- You explicitly want re-dispatch

## When Reclaim+Block Is Required

- Results files exist in workspace
- Output log has completion markers
- Work is done, just missing the completion signal
