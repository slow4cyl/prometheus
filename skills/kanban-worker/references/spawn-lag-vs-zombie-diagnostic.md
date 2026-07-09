# Spawn Lag vs True Zombie — Diagnostic Pattern

## Problem

When the Director runs zombie detection (`ps aux` cross-referenced against `kanban list --status running --json`), tasks that were recently dispatched may have alive worker processes but NOT appear in the running list yet. This is **spawn lag** — the dispatcher spawned the worker, but the list view hasn't caught up.

The naive zombie check (`task_id not in running_ids → ZOMBIE`) produces **false positives** for these recently spawned tasks. Killing them wastes healthy workers.

## Observed in the Wild (Cycle #244)

- 7 tasks had alive worker processes (CPU 0-2s) but were NOT in `kanban list --status running`
- All 7 were legitimate running tasks — just dispatched moments before the Director ran
- The 16 tasks IN the list had CPU 2-5s (slightly older dispatches)
- All 23 tasks were distinct experiment IDs — no duplicates

## Diagnostic: CPU Time

The key differentiator is CPU time:

| Signal | Interpretation | Action |
|--------|---------------|--------|
| Process exists, CPU < 5s | Recently spawned, list lag | Leave alone — will appear in list soon |
| Process exists, CPU 30-60m, 0 heartbeats | Possibly hung | Check workspace output before blocking |
| Process exists, CPU > 60m, 0 heartbeats, no output | Almost certainly stuck | Block with reason |
| No process | Crashed/dead | Block immediately |

## Corrected Detection Pattern

```python
import subprocess, json, re

result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
running = json.load(open('/tmp/kanban_running.json'))
running_ids = {t['id'] for t in running}

# Build process map: task_id -> (worker_id, cpu_time_str)
processes = {}
for line in result.stdout.split('\n'):
    if 'hermes' in line and 'kanban task' in line:
        m_task = re.search(r'kanban task (t_\w+)', line)
        m_worker = re.search(r'prometheus-worker-(\d+)', line)
        if m_task and m_worker:
            tid = m_task.group(1)
            worker = int(m_worker.group(1))
            parts = line.split()
            cpu = parts[9] if len(parts) > 9 else "0:00"
            processes.setdefault(tid, []).append((worker, cpu))

# Classify
for tid, workers in processes.items():
    in_list = tid in running_ids
    # Parse CPU time (format: H:MM or MM:SS)
    cpu_str = workers[0][1]
    # ... classify based on in_list + CPU time
```

## Key Insight

**Not-in-list + alive process + low CPU = spawn lag, NOT zombie.**

The Director should:
1. First check all not-in-list processes for CPU time
2. Only flag as zombie if process is dead OR CPU is high with no output
3. Newly spawned tasks (low CPU) should be ignored — they'll appear in the list on the next tick

## Free Worker Count Impact

When 7 tasks are alive but not in the list, the Director's free worker count is wrong:
- List shows 18 running → Director thinks 4 free
- Actual: 18 + 7 = 25 running → only 3 free (or fewer)

The batch creator may also miscount. Always verify with `ps aux` after batch creation.
