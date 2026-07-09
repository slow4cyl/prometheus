# Pitfall: Scripts Written But Never Executed (added 2026-06-01)

## Pattern

A worker writes multiple Python script files to the workspace but the process dies before executing any of them. The workspace shows only `.py` files (sometimes 3-5 versions like `exp_v2.py`, `exp_v3.py`) with zero output files. `ps aux` shows no live process.

**Distinct from "alive but silent"**: Here the process is DEAD, not alive-but-hung.

## Detection

- Workspace has multiple `.py` files (often v2, v3, v4 variants)
- Zero `*_output.log` or `results.json` files
- Process not in `ps aux`
- Task running >20 min with 0 heartbeats

## Real-World Example (2026-06-01 Director Pass)

Six tasks exhibited this pattern simultaneously:

| Task | Exp | Age | Files | Output |
|------|-----|-----|-------|--------|
| t_2d0da148 | exp_987 | 74m | 5 .py files (v2, v3, v4, test) | None |
| t_96cd7c38 | exp_990 | 74m | 1 .py file | None |
| t_1a450be2 | exp_996 | 68m | 2 files (script + test) | None |
| t_d343f742 | exp_1012 | 46m | 1 .py file | None |
| t_a3307f51 | exp_1027 | 26m | 1 .py file | None |

exp_959 was a variant: 13 files including partial output (died mid-collection), so it had SOME output but wasn't complete.

## Root Causes

1. **Script-writing loop**: Worker spent entire runtime debugging syntax errors or trying different approaches, never reaching execution
2. **Dependency issues**: Missing module, bad import, wrong Python version
3. **API timeout during first call**: Script started but hung on the first API call that never returned
4. **TIRITH filter blocks**: Script content triggered a security filter, worker tried multiple rewrites

## Action

1. `hermes kanban reclaim <task_id>` — release worker claim
2. `hermes kanban block <task_id> "hung: scripts written but never executed, <N> .py files, zero output"` — prevent re-dispatch

## Reuse Opportunity

The scripts in the workspace may be partially correct. A future worker can:
- Read the comment thread to understand what went wrong
- Fix the script rather than starting from scratch
- Reuse test data or API call patterns from the abandoned scripts
