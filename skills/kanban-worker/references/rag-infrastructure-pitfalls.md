# RAG Infrastructure Pitfalls

## INSERT OR REPLACE Without UNIQUE Constraint (June 2026)

The `documents` table in rag.db had no UNIQUE constraint on `source_path`.
Every reindex via `INSERT OR REPLACE` created a NEW row instead of updating,
because `id` is an autoincrement PRIMARY KEY — `INSERT OR REPLACE` on an
autoincrement PK always inserts.

**Result:** 18,615 rows for 2,363 unique files. Each reindex added another copy.

**Detection:** `SELECT source_path, COUNT(*) FROM documents GROUP BY source_path HAVING COUNT(*) > 1`

**Fix applied June 3 2026:**
1. `CREATE UNIQUE INDEX idx_docs_unique_path ON documents(source_path)`
2. Dedup: `DELETE FROM documents WHERE rowid NOT IN (SELECT MAX(rowid) FROM documents GROUP BY source_path)`
3. VACUUM

**Prevention:** Always add UNIQUE constraint on the natural key before using INSERT OR REPLACE.
SQLite's `INSERT OR REPLACE` on an autoincrement PK is an INSERT, not a REPLACE.

## Title/Preview Extraction (June 2026)

The `index_experiments()` function stored `fpath.stem` (filename stem) as the title
for ALL documents. Even though `extract_json_fields()` extracted structured content,
it was used for the embedding text, not the title field.

**Result:** Workers saw "exp_2818_results" instead of "Llama-4's 83.3% ceiling..."

**Fix:** Added `extract_title()` and `extract_preview()` functions that pull meaningful
titles/previews from JSON (hypothesis/title fields), MD (first heading), PY (docstring),
TXT (first meaningful line). Changed `index_experiments()` to use these instead of `fpath.stem`.

**Reindex command:** `python3 ~/.hermes/scripts/experiment_rag.py index --force`

## Preview Quality for JSON Files

Before fix: JSON files without hypothesis/title fields got raw JSON as their preview.
Workers saw `{"experiment": "exp_327", "budget": 128, "domain": "DICT"...}` instead of
a meaningful summary.

After fix: `extract_preview()` tries hypothesis → result → domain for JSON files.
Files without any structured fields fall back to first meaningful line.
~84% of documents now have useful previews.
