# Kanban Utilization Health Check

## Understanding Worker Count Fluctuation

Worker count naturally oscillates. This is by design, not a bug.

**Architecture:** Workers are spawned per-task, not as a fixed pool. The dispatcher
checks for `ready` tasks and spawns one worker process per task. Active worker count
= number of tasks in `running` status.

**Why it fluctuates (5-22 range):**
1. Director creates tasks in batches of ~5-9 per 1-minute cycle
2. Workers claim tasks immediately, count spikes
3. Tasks complete at different rates (30s to 5+ min), count drops
4. Next Director cycle creates more, count rises
5. Director alternates between "create experiments" and "synthesize/assess" modes

**Key insight:** 32 is the ceiling (batch_create_tasks.py WORKERS list), but you only hit it when
32+ independent ready tasks exist with no dependency gates AND the Director is in
full fan-out mode. In practice, the system oscillates because the Director alternates
between creation (count rises) and synthesis (count drops). Note: 10 profiles exist
but only workers 1-32 are in the batch creator's WORKERS list. Workers 33-50 can
only receive tasks via manual Director assignment.

**Updated June 2026:** The Director now uses a dynamic batch size based on free worker
count and curiosity scorer scores (not a hardcoded 5-10 cap). See the director-loop skill's
`references/thread-lock-in-discovery.md` for the full rationale. The old ≥7 saturation
threshold was replaced with a 5-tier decision matrix that accounts for free workers.

## Diagnostic Commands

### Why Are Workers Idle When Tasks Exist?

When ready tasks exist but workers aren't picking them up, check these in order:

```bash
# 1. Check task statuses
sqlite3 ~/.hermes/kanban.db "SELECT status, COUNT(*) FROM tasks GROUP BY status;"

# 2. Check which workers are actually running
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u

# 3. Check ready tasks and their assignments
sqlite3 ~/.hermes/kanban.db "SELECT id, title, status, assignee FROM tasks WHERE status='ready';"

# 4. Check dispatch output for deferrals
hermes kanban dispatch 2>&1
```

**Common causes of idle workers with ready tasks:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Deferred (per-profile cap)" | Task assigned to worker that already has a running task | Wait for current task to complete, or reassign to free worker |
| "Deferred (non-spawnable)" | Task assigned to unknown profile name | `hermes kanban assign <id> prometheus-worker-N` |
| Ready tasks with NULL assignee | Task created without --assignee | `hermes kanban assign <id> prometheus-worker-N` |
| Many ready, few running | Dispatcher may be stalled | `hermes gateway restart` |
| batch_create created 0 tasks | No queue items above min-score threshold | Lower --min-score or check queue quality |

**Per-profile cap check:** If `max_in_progress_per_profile: 1` is set (recommended), each worker can only run 1 task at a time. A ready task assigned to a busy worker will be deferred. This is correct behavior — the task waits for the worker to finish.

### Quick Status
```bash
# Task status breakdown
hermes kanban list --json | python3 -c "
import json,sys
from collections import Counter
tasks = json.load(sys.stdin)
counts = Counter(t['status'] for t in tasks)
for s,c in sorted(counts.items()): print(f'  {s}: {c}')
print(f'  TOTAL: {len(tasks)}')
"

# Worker utilization
hermes kanban list --json | python3 -c "
import json,sys
from collections import Counter
tasks = json.load(sys.stdin)
running = [t for t in tasks if t['status']=='running']
assignees = Counter(t.get('assignee','?') for t in running)
print(f'Running: {len(running)}/22 = {len(running)/22*100:.0f}%')
for a,c in assignees.most_common(): print(f'  {a}: {c}')
"
```

### Heartbeat Health (requires SQLite access to prometheus.db)
```python
import sqlite3, os, time
db = sqlite3.connect(os.path.expanduser('~/.hermes/prometheus.db'))
db.row_factory = sqlite3.Row
rows = db.execute('''
    SELECT t.id, t.assignee, t.started_at,
           h.timestamp as last_hb
    FROM tasks t LEFT JOIN (
        SELECT task_id, MAX(timestamp) as timestamp
        FROM heartbeats GROUP BY task_id
    ) h ON t.id = h.task_id
    WHERE t.status = 'running'
''').fetchall()
now = time.time()
for r in rows:
    age = now - (r['last_hb'] or r['started_at'])
    healthy = r['last_hb'] and (now - r['last_hb']) < 300
    print(f"  {r['id']} | {r['assignee']} | {age/60:.0f}min | {'OK' if healthy else 'STALE'}")
```

### Task Age Distribution
```bash
hermes kanban list --json | python3 -c "
import json,sys,time
tasks = json.load(sys.stdin)
now = time.time()
running = [t for t in tasks if t['status']=='running']
for t in running:
    age = now - t.get('started_at', now)
    print(f'  {t[\"id\"]} | {t[\"assignee\"]} | {age/60:.0f}min | {t[\"title\"][:60]}')
stuck = [t for t in running if now - t.get('started_at',now) > 1800]
if stuck: print(f'WARNING: {len(stuck)} tasks >30min old')
"
```

### Reclaim Loop Detection
```bash
# Check individual task for multiple runs (reclaim/retry cycle)
hermes kanban show <task_id> --json | python3 -c "
import json,sys
d = json.load(sys.stdin)
runs = d.get('runs',[])
reclaimed = sum(1 for r in runs if r.get('status')=='reclaimed')
print(f'Total runs: {len(runs)}, Reclaimed: {reclaimed}')
if reclaimed >= 2: print('POTENTIAL RECLAIM LOOP')
"
```

### Completion Rate (last 12h)
```bash
hermes kanban list --json | python3 -c "
import json,sys,time
from collections import defaultdict
tasks = json.load(sys.stdin)
now = time.time()
hourly = defaultdict(int)
for t in tasks:
    if t['status']=='done' and t.get('completed_at'):
        hourly[int(t['completed_at']//3600)] += 1
cur = int(now//3600)
for h in range(cur-12, cur+1):
    c = hourly.get(h,0)
    print(f'  hour {h}: {c:3d} {\"#\"*c}')
"
```

## Health Check Interpretation

| Metric | Healthy | Investigate |
|--------|---------|-------------|
| Utilization | 30-80% | <10% (queue empty?) or 100% sustained (saturation?) |
| Task age (running) | <10min avg | >30min (stuck?) |
| Reclaims per task | 0-1 | 2+ (reclaim loop) |
| Completion rate | 20-60/hour | <5/hour (Director stalled?) |
| Queue items | 10+ active | <5 (running out of work?) |
| Ready tasks | 0-5 | >10 (dispatcher not claiming? check deferrals) |
| Ready but deferred | 0 | >3 (per-profile cap bottleneck or unknown assignee) |

## Common Patterns That Look Abnormal But Aren't

- **Count drops to 0 between Director cycles:** Normal. Director creates batch → workers
  complete → gap → next batch.
- **One task running much longer than others:** Normal if it's a complex experiment.
  Investigate only if >30min with no heartbeats.
- **Same worker profile assigned multiple tasks:** With `max_in_progress_per_profile: 1` (recommended), the dispatcher defers the second task. Without the cap, the dispatcher spawns unlimited processes per profile — this causes duplicate work and resource waste. Always set the cap in config.yaml.
- **Synthesis task alongside experiments:** Normal. Synthesis worker handles state
  consolidation while experiment workers run in parallel.
