# Director Full Diagnostics — Usage Guide

**Script:** `scripts/director_full_diagnostics.py`

## When to Run

At the **start of every Director pass**, BEFORE doing anything else. This replaces 8+ ad-hoc diagnostic scripts with one consolidated check.

## What It Does

1. **Dumps board state** → `/tmp/kanban_{running,done,ready,blocked}.json`
2. **Checks worker availability** → busy vs free workers (1-22) via `ps aux`
3. **Detects zombies** → processes alive with `kanban task` but not in running list
4. **Detects status desync** → tasks in running list but actually done (calls `kanban show` per task)
5. **Checks unsusnthesized experiments** → done task IDs minus self_state.json IDs
6. **Outputs decision hints** → saturation threshold, synthesis needs, zombie cleanup

## Usage

```bash
# Full check (includes per-task status desync — N API calls)
python3 ~/.hermes/skills/devops/kanban-worker/scripts/director_full_diagnostics.py

# Fast mode (skip status desync — saves N API calls)
python3 ~/.hermes/skills/devops/kanban-worker/scripts/director_full_diagnostics.py --fast
```

## Why --fast?

The status desync check calls `kanban show` for EVERY running task. With 20+ running tasks, that's 20+ API calls just for diagnostics. Use `--fast` when:
- Running count is high (>10)
- You don't need exact verified_running count
- You just need the board snapshot and worker availability

## Output Example

```
=== DIRECTOR FULL DIAGNOSTICS — 2026-06-02 21:30 UTC ===

[1/5] Board state dumped to /tmp/kanban_*.json
[2/5] WORKER AVAILABILITY:
       Busy: 13 workers [2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16]
       Free: 9 workers [1, 3, 7, 17, 18, 19, 20, 21, 22]

[3/5] ZOMBIES: 0 (none)
       Synthesis process alive: True

[4/5] STATUS DESYNC: 2 tasks actually done but listed as running
       t_xxxxxxxx: exp_2040: ...
       t_yyyyyyyy: exp_2031: ...
       Verified running: 20

[5/5] UNSYNTHESIZED: 3 experiments
       Done task IDs with exp_*: 1616
       Self-state experiment IDs: 1902
       exp_1775b
       exp_1776b
       exp_2029

=== SUMMARY ===
Running tasks:      22
Free workers:       9
Unsynthesized:      3
Zombies:            0
Status desync:      2
Synthesis running:  True

=== DECISION HINTS ===
SATURATION: 20 verified running tasks >= 7 threshold
  → SYNTHESIZE only, skip experiment task creation
  → EXCEPTION: 9 free workers + 3 unsynth → create SYNTHESIS task
SYNTHESIS NEEDED: 3 unsynthesized experiments
```

## Decision Hints Explained

- **SATURATION**: Verified running >= 7 → don't create experiment tasks, only synthesis/maintenance
- **BELOW SATURATION**: Verified running < 7 with free workers → create tasks for queue items
- **SYNTHESIS NEEDED**: 3+ unsynthesized → always create synthesis task (regardless of saturation)
- **ZOMBIE CLEANUP**: Kill zombie PIDs, don't reclaim (task is already done)

## Replaces These Ad-Hoc Scripts

The script consolidates functionality from these patterns that Directors previously wrote ad-hoc:
- Board state dump (`hermes kanban list --status {X} --json > /tmp/kanban_{X}.json`)
- Worker availability (`ps aux | grep prometheus-worker`)
- Zombie detection (`ps aux | grep kanban task` vs running list)
- Status desync (`kanban show --json` per task to verify actual status)
- Unsusnthesized detection (done_exp_ids minus ss_exp_ids from self_state.json)
- Decision hints (saturation threshold, synthesis needs)
