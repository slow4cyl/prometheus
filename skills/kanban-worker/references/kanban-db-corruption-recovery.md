# kanban.db Corruption Recovery

## Error Signature

```
kanban: could not initialize database: Refusing to open corrupt kanban DB at
~/.hermes/kanban.db: integrity_check returned 'wrong # of entries in index
idx_events_task'. Original preserved; backup at
~/.hermes/kanban.db.corrupt.<hash>.bak.
```

## Root Cause

Concurrent writes from multiple kanban workers, or a crash during a write transaction, can leave the SQLite index out of sync with the data. The actual data rows are usually intact — only the index metadata is corrupt.

## Fix Sequence

```bash
# 1. Find the most recent backup
ls -lt ~/.hermes/kanban.db.corrupt.*.bak | head -3

# 2. Copy the most recent backup over the corrupt db
cp ~/.hermes/kanban.db.corrupt.<hash>.bak ~/.hermes/kanban.db

# 3. Rebuild the index
sqlite3 ~/.hermes/kanban.db "REINDEX;"

# 4. Verify integrity
sqlite3 ~/.hermes/kanban.db "PRAGMA integrity_check;"
# Should return: ok

# 5. Test
hermes kanban list --status running --json | head -3
```

## Fallback: If All Backups Are Corrupt

```bash
# The .rebuild file is older but often clean
cp ~/.hermes/kanban.db.rebuild ~/.hermes/kanban.db
sqlite3 ~/.hermes/kanban.db "REINDEX;"
sqlite3 ~/.hermes/kanban.db "PRAGMA integrity_check;"
```

## Post-Recovery Verification

After restoring from backup, the database state may be stale (missing recent task creations/completions). Verify:

1. **Running tasks vs actual processes:**
   ```bash
   # Database says:
   hermes kanban list --status running --json > /tmp/db_running.json
   
   # Actually running:
   ps aux | grep 'hermes' | grep kanban | grep -oE 'kanban task [a-z0-9_]+' | sort -u
   ```

2. **Reconcile differences:**
   - Tasks in DB but not in `ps aux` → zombie/completed, verify with `kanban show <id> --json`
   - Tasks in `ps aux` but not in DB → lost during restore, re-create if still needed

3. **Check done count:**
   ```bash
   hermes kanban list --status done --json | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"
   ```
   Compare against expected count from self_state.json.

## Prevention

- Avoid running multiple Director/cron jobs simultaneously that both write to the kanban DB
- The kanban system uses SQLite which handles concurrent reads well but serializes writes — heavy write loads from 20+ workers can trigger this
- Regular `PRAGMA integrity_check` in monitoring can catch corruption early

## When This Happened

- 2026-06-01: Director pass found `wrong # of entries in index idx_events_task`. Fixed by restoring from backup + REINDEX. The most recent backup was also corrupt (same error), so fell back to the `.rebuild` file from 00:55 which was clean but stale.
