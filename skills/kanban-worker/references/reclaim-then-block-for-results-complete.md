# Reclaim-then-Block for Results-Complete Tasks

## Problem

A worker finishes an experiment (results.json, output.log present and complete) but never calls `kanban_complete()`. The task stays `running` with 0 heartbeats, 0 completions, and a stale workspace. The process may still be alive (zombie) or already exited.

This is distinct from a "hung" task (alive but producing nothing) — here the work IS done, the completion signal is missing.

## Detection

1. **Workspace has results files** — `results.json`, `output.log` with completion markers (`Completed:`, `Results saved to:`)
2. **Task status is `running`** in `kanban list`
3. **0 heartbeats, 0 completions** in `kanban show --json`
4. **Process may or may not be alive** — check `ps aux | grep <task_id>`

Key differentiator from "hung" task: the workspace HAS output files. Hung tasks have ZERO output files.

## Response Pattern

```
Step 1: Reclaim  →  hermes kanban reclaim <task_id>
Step 2: Block    →  hermes kanban block <task_id> "results complete from prior run — <filename> exists in workspace"
```

**CRITICAL: Always block immediately after reclaim.** If you only reclaim, the dispatcher treats the task as `ready` and re-dispatches a new worker — wasting a worker slot on work that's already done. See "reclaimed duplicates get re-dispatched" pitfall in main SKILL.md.

## Why Not Just Let It Run?

The zombie process wastes a worker profile. More importantly, if the dispatcher re-dispatches, a new worker will re-run the same experiment from scratch (ignoring existing results), burning API budget for duplicate work.

## Synthesis Implications

The results in the workspace are still valid for synthesis. The synthesis worker can read `results.json` directly from the workspace — the kanban task being "blocked" doesn't invalidate the experiment output. Tag the synthesis task with a comment noting the workspace path.

## Real-World Example (Cycle #225)

```
exp_891 (t_58b2c9c3): Results saved at 12:57, process still alive at 13:05.
  → Reclaimed. Process completed naturally after reclaim. Status flipped to done.
  → No block needed (task resolved itself).

exp_893 (t_6f9d90cf): Results saved at 12:59, process still alive at 13:05.
  → Reclaimed. Dispatcher immediately re-dispatched (task went to ready).
  → Had to block: "results complete from prior run — exp_893_results.json exists"
  → Prevented wasted worker re-running identical experiment.
```

**Lesson:** Always pair reclaim with block for results-complete tasks. The reclaim-only pattern is only safe when you WANT re-dispatch (e.g., transient API failure, hypothesis still valid).
