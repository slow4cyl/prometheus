# Director Pass #250 — Worked Example

**Date:** 2026-06-03
**Starting state:** 20 running tasks, 2813 done, 47 queue items, 4 free workers
**Ending state:** 24 running tasks, 2813 done, synthesis dispatched

## Step-by-step

### 1. Board state dump (TIRITH-safe two-step)
```bash
hermes kanban list --status running --json > /tmp/kanban_running.json
hermes kanban list --status done --json > /tmp/kanban_done.json
```

### 2. Worker availability
```bash
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
# Result: 21 unique workers visible → 1 free (22 - 21)
```

### 3. Unsynthesized detection
```python
# Handle self_state.json where experiments = int, not dict
ss = json.load(open('self_state.json'))
ecl = ss.get('experiments_completed_list', [])  # Top-level key
ss_exp_ids = set()
for e in ecl:
    if isinstance(e, dict): ss_exp_ids.add(e.get('id',''))
    elif isinstance(e, str):
        m = re.search(r'exp_(\d+\w*)', e)
        if m: ss_exp_ids.add(f'exp_{m.group(1)}')
    elif isinstance(e, int): ss_exp_ids.add(f'exp_{e}')

# Cross-reference with done task titles
unsynth = done_exp_ids - ss_exp_ids
# Result: 4 unsynthesized (exp_182, exp_811_v2, exp_2448, exp_2527)
```

### 4. Zombie detection
```python
# Cross-reference ps aux worker processes against kanban list
# 3 tasks in running list had no worker process → already done
# Status desync: list endpoint stale, kanban show confirmed done
```

### 5. Task creation
- Batch creator: `python3 ~/.hermes/scripts/batch_create_tasks.py --count 3` → 3 tasks
- Second batch: `--count 6 --min-score 30` → 6 tasks
- Synthesis task: manually created for 4 unsynthesized experiments

### 6. Dispatch
```bash
hermes kanban dispatch  # Spillover normal — only 1 spawned on first call
```

## Key lessons
- `experiments_completed_list` is top-level, not under `metrics` (despite director-loop fix pattern)
- Synthesis task detection must use `SYNTHESIS:` prefix, not `'synth' in title.lower()`
- Batch creator spillover is normal — remaining tasks picked up on next tick
- 3 zombies found via ps aux cross-reference, not via list endpoint
