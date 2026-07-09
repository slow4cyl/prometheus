# Director Pass: Reclaim-then-Block Decision Flow

During a Director pass, when inspecting running tasks and finding ones with complete results but no completion signal:

## Decision Tree

```
Task running > 30min, 0 heartbeats, 0 completions
  │
  ├─ Workspace has results.json/output.log with completion markers?
  │   ├─ YES → "Results complete but not completed"
  │   │         ACTION: reclaim + immediately block
  │   │         BLOCK REASON: "results complete from prior run — <file> exists"
  │   │         NOTE: synthesis can still read workspace files
  │   │
  │   └─ NO → Check process liveness (ps aux)
  │       ├─ Process alive → "Alive but silent" (hung)
  │       │   ├─ Workspace has output files → long computation, leave alone
  │       │   └─ Workspace has ONLY the script → hung, block it
  │       │
  │       └─ Process dead → crashed, block immediately
```

## Key Principle

**Always pair reclaim with block for results-complete tasks.** Reclaim-only puts the task back to `ready`, causing the dispatcher to re-dispatch. The new worker will re-run the experiment from scratch, wasting API budget on duplicate work.

## Exception

Reclaim-only is correct when you WANT re-dispatch: the hypothesis is sound, the task body is complete, and you just need a fresh worker (e.g., transient API failure). But for "results complete" tasks, the work is done — block it.

## Hung Task Sequence: Kill → Reclaim → Block (added Cycle #245)

When a task is confirmed hung (zero CPU on all processes, stale workspace, 300+ min runtime), the correct sequence is:

1. **Kill processes first** — prevents the hung process from consuming resources during reclaim
2. **Reclaim** — releases the worker claim so kanban status changes from "running" to "ready"
3. **Block** — prevents dispatch from re-spawning the task

**Why kill before reclaim:** If you reclaim without killing, the OS process continues running independently (it was launched as a shell subprocess, not managed by kanban lifecycle). The process consumes API budget on work nobody will read.

**Why block after reclaim:** Reclaim puts the task in "ready" status. Without blocking, dispatch re-spawns a worker for the same hung task on the next tick.

```bash
# Correct sequence for hung tasks
HERMES_PID=$(ps aux | grep "kanban task $tid" | grep -v grep | awk '{print $2}' | head -1)
pgrep -P $HERMES_PID 2>/dev/null | xargs -r kill -9  # kill children
kill -9 $HERMES_PID                                    # kill agent
hermes kanban reclaim $tid                              # release claim
hermes kanban block $tid "hung: reason"                 # prevent re-dispatch
```

**Contrast with "results complete" tasks:** For tasks with complete output but no completion signal, you can skip the kill step (the process may have already exited or be doing harmless cleanup). Just reclaim + block.
