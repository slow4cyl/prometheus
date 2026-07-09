# Heartbeat System Diagnostic & Fallback (Cycle #245)

## Problem

The heartbeat system (`heartbeat_writer.py` + `prometheus.db heartbeats` table) may be non-functional. In Cycle #245, zero heartbeats were recorded for any of 42 running tasks despite workers being alive and consuming CPU.

## Root Causes (check in order)

1. **System overload (most common):** When 30+ CPU-heavy experiments run simultaneously on a 16-core machine, load average spikes to 100+. Workers die or time out before they reach the heartbeat call. The heartbeat infrastructure is fine — the workers never survived long enough to use it. **Indicator:** load average > 2× cores, 100+ experiment processes running. **Fix:** Kill excess experiments, pause Director, let system stabilize. See `references/scaling-to-50-workers.md` in kanban-orchestrator skill for the full procedure.

2. **SQLite lock contention (rare with busy_timeout):** kanban_db has 120s busy_timeout, prometheus_db has 5s. Unlikely to cause heartbeat failures unless the database is corrupted. **Indicator:** `database is locked` errors in logs.

3. **Workers not calling heartbeat_writer.py:** Task body doesn't include heartbeat instructions, or workers skip the heartbeat step. **Indicator:** some workers have heartbeats, others don't.

4. **heartbeat_writer.py script error:** The script itself fails (wrong env vars, missing prometheus_db module). **Indicator:** heartbeat_writer processes visible but no entries in DB.

## Diagnosis

```sql
-- Check if ANY heartbeats exist for running tasks
SELECT t.id, t.title,
       (SELECT MAX(h.timestamp) FROM heartbeats h WHERE h.task_id = t.id) as last_hb
FROM tasks t WHERE t.status='running'
ORDER BY t.created_at ASC;
-- If all show NULL for last_hb, heartbeat system is broken
```

```bash
# Check if heartbeat_writer.py is being called
ps aux | grep heartbeat_writer | grep -v grep
# If no processes, workers aren't invoking it
```

```sql
-- Check total heartbeat count
SELECT COUNT(*) FROM heartbeats;
-- If 0 or very low relative to task count, system is broken
```

## Fallback: Age-Based Zombie Detection

When heartbeats are unavailable, use task age as the primary signal:

1. **Query running tasks with creation time:**
   ```sql
   SELECT id, title, created_at FROM tasks WHERE status='running' ORDER BY created_at ASC;
   ```

2. **Classify by age:**
   - `< 10 min`: Likely alive (just spawned)
   - `10-30 min`: Check if worker process exists (`ps aux | grep <assignee>`)
   - `30-60 min`: Suspicious — check worker process AND CPU usage
   - `> 60 min`: Almost certainly dead — reclaim

3. **Verify worker liveness independently:**
   ```bash
   ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
   ```
   Compare active worker set against task assignees. Tasks assigned to workers not in the active set are zombies.

4. **Cross-check with task_runs:**
   ```sql
   SELECT id, task_id, profile, status, started_at, ended_at
   FROM task_runs WHERE task_id = '<task_id>' ORDER BY id DESC LIMIT 3;
   ```
   If the latest run shows `status='running'` but the worker process is dead, the task is a zombie.

## Key Insight

Heartbeat-based detection is precise (knows exactly when a worker died). Age-based detection is imprecise (a slow API call looks like a dead worker). When heartbeats are broken, err on the side of longer age thresholds (60+ min) to avoid reclaiming legitimately slow tasks.

## Prevention

Workers should call `heartbeat_writer.py` at the top of their script:
```bash
python3 ~/.hermes/scripts/heartbeat_writer.py "$HERMES_KANBAN_TASK" "$HERMES_PROFILE" &
```
If workers aren't doing this, the task body should include explicit heartbeat instructions.
