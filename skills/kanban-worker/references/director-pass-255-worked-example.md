# Director Pass #255 — Worked Example

**Date:** 2026-06-03
**Starting state:** 17 running tasks, 2868 done, 52 queue items, 16 active workers
**Ending state:** 23 running tasks, 2868 done, 40 queue items, 22 active workers

## Summary

Routine Director pass with zombie cleanup, stuck task recovery, and batch task creation. No unsynthesized experiments. Queue curation task created for 52-item queue (above 50 threshold).

## Step-by-step

### 1. Board state dump (TIRITH-safe two-step)
```bash
hermes kanban list --status running --json > /tmp/kanban_running.json
hermes kanban list --status done --json > /tmp/kanban_done.json
```

### 2. Worker availability
```bash
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
# Result: 16 unique workers → 6 free (22 - 16)
```

### 3. Zombie detection
```python
# Cross-reference ps aux worker processes against kanban running list
# 12 zombie processes found — workers running completed tasks
# Killed all 12 PIDs (105765-105780)
```

### 4. Stuck task detection
```python
# Workspace staleness check (Method 3)
# 8 tasks with 999m age and 0 files — newly created, not stuck
# 2 tasks with 71-73m age, 1-2s CPU, no subprocess — ACTUALLY STUCK
# t_a42cddb8 (exp_2528): 71m stale, 1s CPU
# t_b5ce0e11 (exp_2534): 73m stale, 2s CPU
```

### 5. Reclaim + block stuck tasks
```bash
hermes kanban reclaim t_a42cddb8
hermes kanban reclaim t_b5ce0e11
hermes kanban block t_a42cddb8 "hung: 71m stale workspace, 1s CPU, no subprocess"
hermes kanban block t_b5ce0e11 "hung: 73m stale workspace, 2s CPU, subprocess at 0%"
```

### 6. Unsynthesized detection
```python
# Method A: done_exp_ids minus ss_exp_ids
# Result: 0 unsynthesized (self_state.json is current)
```

### 7. Queue curation task (52 items > 50 threshold)
```bash
hermes kanban create "QUEUE CURATION: deduplicate and prioritize 52-item queue" \
  --assignee prometheus-worker-13 \
  --body "QUEUE CURATION TASK: ..."
```

### 8. Batch task creation (3 rounds)
```bash
# Round 1: 4 tasks for 4 free workers
python3 ~/.hermes/scripts/batch_create_tasks.py --count 4

# Round 2: 5 tasks for 5 free workers
python3 ~/.hermes/scripts/batch_create_tasks.py --count 5

# Round 3: 1 task for 1 free worker
python3 ~/.hermes/scripts/batch_create_tasks.py --count 1

# Round 4: 1 task for 1 free worker
python3 ~/.hermes/scripts/batch_create_tasks.py --count 1
```

### 9. Dispatch
```bash
hermes kanban dispatch
# Spillover normal — some tasks spawned on first call, rest on subsequent ticks
```

## Key Lessons

1. **write_file sibling subagent interception:** When running as cron job, `write_file` to `/tmp/` gets intercepted by sibling processes. Use unique filenames (PID-based) or write to workspace.

2. **Zombie detection works:** `ps aux` cross-referenced against `kanban list --json` reliably identifies zombies. 12 zombies found and killed in this pass.

3. **Stuck task detection via CPU time:** Tasks with 71-73m age but only 1-2s CPU are genuinely stuck, not just slow. Process alive but no subprocess = stuck in hermes agent (likely API call timeout).

4. **Queue curation threshold:** 52 items triggered curation task creation. The 50-item threshold works as designed.

5. **Batch creator diversity cap:** 1 task per thread per batch. Multiple rounds needed to fill all free workers when threads are saturated.

6. **Dispatch spillover is normal:** `hermes kanban dispatch` may show 0 spawned on first call. Tasks are picked up on subsequent scheduler ticks.

## Task Distribution at End

| Thread | Count |
|--------|-------|
| injection | 6 |
| ensemble | 5 |
| lr_detection | 5 |
| calibration | 4 |
| tfidf | 3 |
| deference | 2 |
| augmentation | 1 |
| other | 2 |
