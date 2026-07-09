# Pitfall: results.json Written Mid-Experiment (added 2026-06-01)

## Problem

A task can have a large `results.json` (e.g., 80KB) that was written DURING the experiment, not at completion. The experiment continues processing after writing intermediate results. This creates a false "results-complete" signal.

## Example (exp_947, Cycle #228)

- Task `t_dba9c0b3` (exp_947) had `results.json` (80KB) written at 14:08
- Output log showed the experiment was still at item 65 of ~100 at 15:14
- The results.json contained partial data for only one model (qwen/qwen3.6-35b) when the task body specified testing across two models
- Worker process was alive and actively processing — NOT stuck

## Detection

Before concluding "results-complete", check BOTH conditions:

1. **results.json exists** — yes, this is necessary but NOT sufficient
2. **Output log shows completion** — check for:
   - Completion markers: "DONE", "COMPLETE", final accuracy/score line
   - Log staleness: last modification >10min ago with no recent updates
   - Progress indicators: "Attack 65/100..." means still running

If the log is being actively updated and shows incomplete progress, the task is NOT results-complete despite having results.json.

## Fix

Only reclaim+block when BOTH conditions hold:
- (a) results.json exists
- (b) output log shows completion markers OR has not been modified in >10min

## Contrast with Related Patterns

| Pattern | results.json | output.log | Action |
|---------|-------------|------------|--------|
| Results-complete | Present, complete | Shows completion markers | Reclaim + block |
| Mid-experiment | Present, partial | Being actively updated | Leave alone (still running) |
| Alive but silent | Absent or minimal | Absent or stale >60min | Reclaim + block |
| Healthy long computation | May be absent | Being actively updated | Leave alone |

## Key Insight

The presence of `results.json` alone is NOT a reliable completion signal. Many experiment scripts write intermediate results as they process each condition/model. The output log's last modification time and content are the ground truth for whether the experiment is still active.
