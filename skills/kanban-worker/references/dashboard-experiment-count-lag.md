# Dashboard Experiment Count Lag — Complete Diagnostic Guide

**Last major update:** 2026-06-04 (structural gate fix)
**Previous:** 2026-06-02 (initial Variant 2 diagnosis)

## Architecture: The Data Pipeline

```
Worker runs experiment
  → write_worker_result.py → worker_results table (prometheus.db)
  → apply_worker_results.py (cron 2m) → experiments table (prometheus.db)
  → dashboard (port 8888) reads experiments table
  → sync_sqlite_to_state.py (systemd timer 2m) → self_state.json

Separately:
  kanban tasks → sync_kanban_to_db.py → prometheus.db
  kanban tasks → kanban DB (kanban.db)
```

**Critical:** The dashboard reads DIRECTLY from `prometheus.db` (experiments table).
It does NOT read from kanban.db or self_state.json for experiment counts.
The `worker_results` → `apply_worker_results` → `experiments` path is the ONLY
way new experiments reach the dashboard.

## Symptom: Dashboard Count Appears Stale

Kanban done count keeps climbing but dashboard experiment count stays flat.
The dashboard auto-refreshes every 10 seconds (`<meta http-equiv="refresh" content="10">`).

## Known Root Causes (in order of likelihood)

### Root Cause 1: Workers Skip write_worker_result.py (94% of cases)

**This is the most common cause.** Workers complete kanban tasks without ever
calling `write_worker_result.py`. The findings are lost.

**Why it happens:**
- Workers using weak/free-tier models lose instruction adherence over long task bodies
- Task bodies are 200+ lines; `write_worker_result.py` instruction is near the end
- The model runs the experiment, gets a result, then calls `kanban_complete` directly
- The MANDATORY instruction in the task body is behavioral, not structural

**Evidence (June 4 2026):**
- 3,405 done tasks, only 226 had kanban result strings, only 207 had recoverable data
- 3,209 / 3,405 (94%) done tasks had NO result at all
- Last `write_worker_result.py` call was 31 minutes before diagnosis
- 32 workers running, all alive and heartbeating — they just skip the result step

**Diagnosis:**
```sql
-- In kanban.db: how many done tasks have no result?
SELECT
  CASE WHEN result IS NOT NULL AND result != ''
    THEN 'has_result' ELSE 'no_result' END as r,
  COUNT(*)
FROM tasks WHERE status='done'
GROUP BY r;

-- If no_result >> has_result, this is the problem.

-- Cross-check: how many worker_results exist vs done tasks?
-- (Query prometheus.db for worker_results, kanban.db for tasks)
python3 -c "
import sqlite3
kdb = sqlite3.connect('~/.hermes/kanban.db')
pdb = sqlite3.connect('~/.hermes/prometheus.db')
done = kdb.execute(\"SELECT COUNT(*) FROM tasks WHERE status='done' AND title LIKE 'exp_%'\").fetchone()[0]
wr = pdb.execute('SELECT COUNT(*) FROM worker_results').fetchone()[0]
print(f'Done experiment tasks: {done}, Worker results: {wr}')
print(f'Gap (lost results): {done - wr}')
"
```

**Fix (implemented June 4 2026):**

Three-layer fix, all deployed:

1. **Structural gate in `kanban_complete`** (`tools/kanban_tools.py`):
   - Function: `_enforce_worker_result_written(tid)` (line ~163)
   - Called in `_handle_complete()` after ownership check (line ~547)
   - Blocks completion of `exp_*` tasks unless `worker_results` has a row
     with matching `experiment_id` (extracted from task title)
   - Only fires for dispatcher-spawned workers (checks `HERMES_KANBAN_TASK` env)
   - Best-effort: never blocks on DB errors (try/except → return None)
   - Gateway restart required after code change
   - **This is the primary prevention mechanism**

2. **Worker model upgrade** (10 profiles updated):
   - Profiles: `~/.hermes/profiles/prometheus-worker-*/config.yaml`
   - Changed: `default: minimax-m3-free` → `default: xiaomi/mimo-v2.5`
   - Changed: `provider: opencode-zen` → `provider: openrouter`
   - Ensures workers can follow long task body instructions reliably

3. **Recovery sweep** (`~/.hermes/scripts/recovery_sweep.py`):
   - One-time script to recover lost results from kanban task result strings
   - Usage: `python3 ~/.hermes/scripts/recovery_sweep.py [--dry-run]`
   - Finds done tasks with `exp_*` titles, kanban results, no `worker_results` row
   - Parses result text, inserts into `worker_results` table
   - Then run: `python3 ~/.hermes/scripts/apply_worker_results.py`
   - **Limitation:** Only recovers tasks that have kanban result strings.
     Tasks with empty results (workspaces GC'd) are permanently lost.

### Root Cause 2: apply_worker_results.py Not Running

The cron job that applies `worker_results` → `experiments` may have stopped.

**Diagnosis:**
```bash
# Check the cron job
hermes cron list | grep -i apply

# Check worker_results table — are there unapplied rows?
sqlite3 ~/.hermes/prometheus.db "SELECT applied, COUNT(*) FROM worker_results GROUP BY applied;"
# If applied=0 count > 0, the apply script isn't running.

# Manual apply
python3 ~/.hermes/scripts/apply_worker_results.py
```

### Root Cause 3: sync_sqlite_to_state.py Not Running

The systemd timer syncs prometheus.db → self_state.json. If stopped,
self_state.json falls behind.

**Diagnosis:**
```bash
systemctl status prometheus-sync-state.timer
systemctl status prometheus-sync-state.service
# Should show recent "Finished" entries

# Manual sync
python3 ~/.hermes/scripts/sync_sqlite_to_state.py
```

### Root Cause 4: UNIX Timestamp Sort Bug

Mixed timestamp formats cause SQLite string sorting to misorder experiments.

**Diagnosis:**
```sql
SELECT CASE
    WHEN created_at LIKE '2026-%' THEN 'ISO'
    WHEN created_at LIKE '1780%' THEN 'UNIX'
    ELSE 'OTHER'
  END as format, COUNT(*) as count
FROM experiments GROUP BY format;
```

**Fix:** Convert all UNIX timestamps to ISO:
```python
import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('~/.hermes/prometheus.db')
rows = conn.execute(
    "SELECT id, created_at FROM experiments WHERE created_at LIKE '1780%'"
).fetchall()
for row_id, ts in rows:
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    conn.execute(
        'UPDATE experiments SET created_at = ? WHERE id = ?',
        (dt.isoformat(), row_id)
    )
conn.commit()
```

### Root Cause 5: Dashboard Process Issues

**Diagnosis:**
```bash
sudo systemctl is-active prometheus-dashboard.service
curl -s -o /dev/null -w '%{http_code}' http://localhost:8888/
sudo systemctl restart prometheus-dashboard.service
```

## Dashboard Data Sources

The dashboard (`prometheus_dashboard.py`, port 8888) reads:

| Stat | Source | Query |
|------|--------|-------|
| Experiment count | prometheus.db | `experiments` table, filtered by `filter_out_already_answered()` |
| Completed count | prometheus.db | Same, status='completed' |
| Domain count | prometheus.db | `domains` table |
| Skills count | prometheus.db | `skills` table |
| Queue size | self_state.json | `curiosity_queue` array length |
| Curiosities | prometheus.db | `curiosities` table |
| Working memory | WM daemon (port 19876) | HTTP `/status` endpoint |
| Version | prometheus.db | `system` table, key='version' |

**Key filter:** `filter_out_already_answered()` removes experiments tagged
ALREADY_ANSWERED/DUPLICATE or with matching hypothesis prefixes. Dashboard
shows "real" experiments, not total.

## Scripts Reference

| Script | Location | Purpose |
|--------|----------|---------|
| `write_worker_result.py` | `~/.hermes/scripts/` | Workers call this to record findings |
| `apply_worker_results.py` | `~/.hermes/scripts/` | Applies worker_results → experiments table |
| `sync_sqlite_to_state.py` | `~/.hermes/scripts/` | Syncs prometheus.db → self_state.json |
| `sync_kanban_to_db.py` | `~/.hermes/scripts/` | Syncs kanban.db → prometheus.db |
| `recovery_sweep.py` | `~/.hermes/scripts/` | One-time recovery of lost results |
| `prometheus_dashboard.py` | `~/` | Dashboard server (port 8888) |

## Key File Locations

| File | Purpose |
|------|---------|
| `~/.hermes/prometheus.db` | Main experiments database (WAL mode) |
| `~/.hermes/kanban.db` | Kanban task database |
| `~/.hermes/self_state.json` | Agent state (synced from prometheus.db) |
| `~/.hermes/hermes-agent/tools/kanban_tools.py` | Kanban tool handlers (contains the gate) |
| `~/.hermes/profiles/prometheus-worker-*/config.yaml` | Worker profile configs (model, provider) |
| `~/.hermes/skills/devops/kanban-worker/SKILL.md` | Worker instructions (MANDATORY rules) |
| `~/.hermes/skills/devops/kanban-worker/references/experiment-task-body-template.md` | Task body template |

## Systemd Services

| Service | Purpose | Port |
|---------|---------|------|
| `prometheus-dashboard.service` | Dashboard server | 8888 |
| `wm-daemon.service` | Working memory daemon | 19876 |
| `prometheus-sync-state.timer` | SQLite → self_state sync | — |
| `hermes-gateway.service` | Hermes gateway (runs workers) | — |

## Lessons Learned

1. **Behavioral instructions are not enforcement.** "MANDATORY" in a task body
   is a suggestion. The model may skip it, especially with weak models or
   long contexts. Critical pipeline steps need structural enforcement.

2. **Weak models + long contexts = instruction loss.** Free-tier models
   (minimax-m3-free, etc.) lose adherence over 200+ line task bodies.
   The instruction at the END of the body is most likely to be skipped.

3. **Workspaces are ephemeral.** The workspace-gc cron deletes completed
   task workspaces. By the time you notice missing results, the evidence
   is gone. Only kanban task result strings survive.

4. **The gap between kanban completion and experiment recording is a
   single point of failure.** The `write_worker_result.py` → `apply_worker_results.py`
   pipeline has no redundancy. If the worker skips the write, the data is lost.

5. **The structural gate pattern works.** Adding a check in `kanban_complete`
   that blocks completion unless prerequisites are met is the right pattern
   for any critical pipeline step. Same pattern as `_enforce_worker_task_ownership`.
