# Director Pre-Flight: DB Table Verification

**Added June 5, 2026** — After discovering prometheus.db was missing its experiments table during a Director cycle.

## When to Run

At the START of every Director pass, BEFORE any queue scoring, task creation, or synthesis checks.

## Why This Matters

The `experiments` table can disappear from prometheus.db due to:
- Corruption from concurrent writes
- Botched schema migrations
- Disk issues

When it's missing:
- `queue_curator.py` runs but reports 0 completed experiments (misleading)
- `health_signal.py` shows 0/0 coverage
- `batch_create_tasks.py` can't check coverage against completed experiments
- Synthesis throttle check returns 0/0
- The Director proceeds with task creation anyway, creating duplicates

## Quick Check (add to Director's Step 0)

```python
import sqlite3, os
db_path = os.path.expanduser('~/.hermes/prometheus.db')
db = sqlite3.connect(db_path)
tables = [t[0] for t in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
db.close()
if 'experiments' not in tables:
    print(f"CRITICAL: experiments table missing from prometheus.db!")
    print("Recovery: cp ~/.hermes/backups/prometheus-db/prometheus-$(ls -t ~/.hermes/backups/prometheus-db/ | head -1 | sed 's/prometheus-//;s/.db//') ~/.hermes/prometheus.db")
```

## Recovery

```bash
# 1. Save corrupted file
cp ~/.hermes/prometheus.db ~/.hermes/prometheus.db.corrupt

# 2. Verify backup has the table
python3 -c "
import sqlite3
db = sqlite3.connect('BACKUP_PATH')
tables = [t[0] for t in db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
print(f'Has experiments: {\"experiments\" in tables}')
db.close()
"

# 3. Restore
cp ~/.hermes/backups/prometheus-db/prometheus-$(ls -t ~/.hermes/backups/prometheus-db/ | head -1 | sed 's/prometheus-//;s/.db//') ~/.hermes/prometheus.db
```

## Related

- `backup-restore` skill: `references/prometheus-db-missing-table-recovery.md`
- `director-loop` skill: `references/prometheus-db-missing-table-pitfall.md`
- `director-loop` skill: `references/director-pass-checklist-with-db-recovery.md`
