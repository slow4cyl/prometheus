# sync_sqlite_to_state.py Queue Overwrite Race Condition

**Date:** 2026-06-02
**Severity:** High (dashboard completely useless when triggered)

## The Bug

`sync_sqlite_to_state.py` reads curiosities from SQLite's `curiosities` table
and overwrites `self_state.json`'s `curiosity_queue` array whenever counts differ.

This creates a race condition with any queue repair script:

```
Timeline:
  13:42:00  Queue repair script cleans queue to 25 items in self_state.json
  13:42:01  Repair verified: queue = 25 items ✓
  13:44:00  dashboard_watchdog.py runs (every 2 min)
  13:44:01    calls sync_sqlite_to_state.py
  13:44:02    sync reads 3,986 curiosities from SQLite
  13:44:02    sync sees 3,986 != 25 (count mismatch)
  13:44:02    sync OVERWRITES queue back to 3,986 items ✗
  13:44:03  Dashboard shows 3,986 queue items (repair undone)
```

## Root Cause

Two data sources for the same logical entity:
- `self_state.json` → `curiosity_queue` (managed by Director/repair scripts)
- `prometheus.db` → `curiosities` table (tracking log, written by sync_experiments.py)

The sync script treated SQLite as authoritative for the queue. It isn't.

## The Fix

Remove the queue overwrite from `sync_sqlite_to_state.py` (lines ~140-156):

```python
# BEFORE (broken):
db_curios = conn.execute(
    "SELECT id, text, priority, status FROM curiosities WHERE status='active' ..."
).fetchall()
if db_curios:
    new_queue = [...]
    if len(new_queue) != len(old_queue):
        state["curiosity_queue"] = new_queue  # OVERWRITES REPAIRS

# AFTER (fixed):
# NOTE: self_state.json is the AUTHORITATIVE source for the queue.
# The Director manages the queue. SQLite curiosities is a tracking log.
# Do NOT overwrite the queue from SQLite.
```

## Why self_state.json Is Authoritative

The queue is a dynamic data structure managed by multiple processes:
- **Director**: adds new items from experiment follow-ups
- **Synthesis worker**: marks items as resolved
- **Repair scripts**: deduplicate, unwrap corruption, score priority
- **Curiosity scorer**: re-prioritizes items

SQLite curiosities is a append-only log written by `sync_experiments.py`.
It never deletes resolved items. It never deduplicates. It's a firehose,
not a managed data structure.

## Verification

After fixing, run the full sync cycle and verify queue stability:

```bash
python3 ~/.hermes/scripts/sync_experiments.py
python3 ~/.hermes/scripts/sync_sqlite_to_state.py
python3 -c "
import json
with open(os.path.expanduser('~/.hermes/self_state.json')) as f:
    data = json.load(f)
print(f'Queue: {len(data.get(\"curiosity_queue\", []))} items')
"
```

Queue count should be unchanged after sync.

## Impact on Dashboard

The dashboard reads queue count from `self_state.json`. When the sync
overwrites the queue, the dashboard shows the inflated count (3,986)
instead of the actual cleaned count (25-60). This makes the dashboard
completely useless for monitoring queue health.
