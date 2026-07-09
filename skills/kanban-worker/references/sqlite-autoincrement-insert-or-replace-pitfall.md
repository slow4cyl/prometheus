# SQLite Autoincrement + INSERT OR REPLACE Duplicate Rows Pitfall

## Problem (June 3, 2026)

When a table has `INTEGER PRIMARY KEY AUTOINCREMENT` and no UNIQUE constraint
on other columns, `INSERT OR REPLACE` always **inserts** new rows instead of
replacing existing ones. The conflict is on the `id` column (autoincrement),
which never conflicts — so SQLite treats every INSERT OR REPLACE as a fresh insert.

### Impact on RAG documents table

The `documents` table in `~/.hermes/rag/rag.db` accumulated 18,615 rows for
only 2,363 unique source files. Each reindex cycle (`--force`) created new rows
instead of updating existing ones. Workers saw the **oldest** row's title
(filename stem like "exp_2818_results") instead of the latest extracted title.

### Why it went unnoticed

- `SELECT COUNT(*)` returned 18,615 —看起来 like a rich index
- `INSERT OR REPLACE` is supposed to replace —开发者 assumed it worked
- The query function reads ALL rows and computes similarity for each —
  duplicates scored identically, just appeared multiple times in results
- The `indexed_at` timestamp on old rows was from weeks ago, but the
  reindex log showed "2360 indexed" — the counter incremented for every
  file processed, not for every row actually stored

### Detection

```sql
-- Check for duplicates
SELECT source_path, COUNT(*) as cnt FROM documents
GROUP BY source_path HAVING cnt > 1 LIMIT 10;

-- Compare total vs unique
SELECT COUNT(*) as total, COUNT(DISTINCT source_path) as unique FROM documents;
```

## Fix

### 1. Add UNIQUE constraint on the natural key

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_unique_path
ON documents(source_path);
```

**Must COMMIT immediately** — if you run test inserts and then rollback,
the index creation is also rolled back.

### 2. Deduplicate (keep newest row per source_path)

```sql
DELETE FROM documents WHERE rowid NOT IN (
    SELECT MAX(rowid) FROM documents GROUP BY source_path
);
```

### 3. VACUUM to reclaim space

```sql
VACUUM;
```

### 4. Verify INSERT OR REPLACE now works

```sql
-- Should fail with UNIQUE constraint error on second insert
INSERT INTO documents (doc_type, source_path, title, created_at)
VALUES ('test', '/test/dup', 'test', 0);
INSERT INTO documents (doc_type, source_path, title, created_at)
VALUES ('test', '/test/dup', 'test2', 0);
-- Expected: UNIQUE constraint failed

-- Clean up
DELETE FROM documents WHERE source_path = '/test/dup';
```

## Prevention

Any table that uses `INSERT OR REPLACE` for upsert behavior MUST have a
UNIQUE constraint on the natural key (not just the autoincrement id).
Check before writing:

```python
# Verify UNIQUE index exists
indexes = conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
has_unique = any('unique' in idx[0].lower() for idx in indexes)
if not has_unique:
    print("WARNING: No UNIQUE index — INSERT OR REPLACE will create duplicates")
```

## Affected tables

- `rag.db/documents` — fixed June 3, 2026 (18,615 → 2,363 rows)
- Any future table using `INSERT OR REPLACE` with autoincrement PK

## Related

- `experiment_rag.py` — uses `INSERT OR REPLACE` in `_store_embeddings()`
  and `_store_single_embedding()`
- `batch_create_tasks.py` — reads from this table for dedup
