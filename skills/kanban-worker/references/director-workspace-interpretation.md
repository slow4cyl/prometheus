# Director Workspace Staleness Interpretation Guide

## Context

When running the Director workspace staleness check (Method 3 from kanban-worker), tasks show different workspace states. The interpretation depends on when the task was dispatched.

## State Interpretations

| Workspace State | Process Status | Interpretation | Action |
|---|---|---|---|
| `NO_WORKSPACE` | Alive, dispatched <10 min | Normal — workspace not yet created | Wait 5+ min, re-check |
| `EMPTY` (0 files) | Alive, dispatched <10 min | Normal — worker hasn't written files yet | Wait 5+ min, re-check |
| `EMPTY` (0 files) | Alive, dispatched >20 min | Possible stuck — worker hasn't produced output | Check `ps aux` for subprocess, check `/tmp/` for output logs |
| `ACTIVE(<5m)` | Alive | Worker actively writing — healthy | Leave alone |
| `SLOW(5-15m)` | Alive | Worker in API call or between phases | Check `ps aux` to confirm process alive |
| `STALE(>15m)` | Alive | Two possibilities: (1) long API call, (2) hung | Check workspace file count and `/tmp/` for output logs |
| `STALE(>15m)` | Dead | Worker crashed | Block task |
| Any state | Dead | Worker crashed | Block task |

## Key Pitfalls

1. **"Just dispatched" false positive**: Tasks dispatched in the current Director pass show `EMPTY` or `NO_WORKSPACE`. This is NORMAL. Do NOT diagnose as stuck. Cross-reference with `ps aux` and wait 5+ minutes.

2. **Tee-output blind spot**: Scripts that pipe output via `tee /tmp/exp_XXX_output.log` write to `/tmp/`, NOT the workspace. Workspace shows only the original script file — a false "hung" signal. Check `ls -lt /tmp/exp_*_output.log` when workspace shows 1 file but process is alive.

3. **Stale files from previous run**: Workspace files may be remnants of a prior attempt. Compare file mtimes against process start time from `ps aux`. If `mtime < process_start`, files are stale artifacts.

## Cycle #219 Example

All 22 running tasks checked:
- 12 tasks: `ACTIVE` (files being written, healthy)
- 8 tasks: `EMPTY` (dispatched 5-8 min ago, normal)
- 1 task: `NO_WORKSPACE` (dispatched 8 min ago, normal)
- 1 task: `STALE 25m` but process alive with subprocess producing output to `/tmp/`

Result: 0 stuck tasks. All processes alive.
