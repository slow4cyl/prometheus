# RAG — Experiment Search

A semantic search system exists for finding related past experiments.
**Always query it before starting any experiment.**

## Quick Usage

```bash
python3 ~/.hermes/scripts/experiment_rag.py query "<topic keywords>" --top-k 5 --worker-id worker
```

Returns: title, score (0-1, higher = more relevant), content preview.

## What's Indexed

- ~16,300 experiment documents (from ~/.hermes/experiments/)
- ~2,700 Prometheus experiments (from prometheus.db)
- Embedding model: nomic-embed (768-dim) on GPU port 9150
- ~300MB VRAM, permanent via systemd (gpu-embed.service)
- Index updated every 5 minutes via cron (rag-quality-monitor)

## When to Query

**First step of EVERY experiment.** Before writing code, before designing
methods, before calling APIs. Query RAG, read results, then decide whether
to build on existing work or start fresh.

## Interpreting Results

| Score | Meaning | Action |
|-------|---------|--------|
| > 0.7 | Highly relevant | Read the experiment in full. Build on it. |
| 0.4 - 0.7 | Somewhat related | Check if your question overlaps with the result. |
| < 0.4 | Not relevant | Proceed with your own approach. |

## Graceful Degradation

If the embedding server is down or the query fails:
- Print a warning: "RAG unavailable — proceeding without semantic search"
- Continue with the experiment normally
- Do NOT block, do NOT fail, do NOT retry more than once

The systemd service auto-restarts on failure, but temporary downtime
happens. Experiments must not depend on RAG being available.

## Quality Diagnostics

### Quick health check

```bash
# Embedding server alive?
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9150/health

# Full status:
python3 ~/.hermes/scripts/experiment_rag.py status
```

### Full diagnostic pull

```python
import sqlite3, time
conn = sqlite3.connect('~/.hermes/rag/rag.db')
conn.row_factory = sqlite3.Row

# Index freshness
stats = dict(conn.execute('SELECT key, value FROM index_stats').fetchall())
print(f"Last index: {stats.get('last_index', 'never')}")

# Doc counts
for row in conn.execute('SELECT doc_type, COUNT(*) FROM documents GROUP BY doc_type'):
    print(f"  {row[0]}: {row[1]}")

# 7-day query quality
cutoff = time.time() - 7*86400
row = conn.execute('''
    SELECT COUNT(*) as n, ROUND(AVG(avg_score),4) as avg,
           COUNT(DISTINCT worker_id) as workers
    FROM query_log WHERE queried_at > ?
''', (cutoff,)).fetchone()
print(f"Queries (7d): {row['n']}, avg_score: {row['avg']}, workers: {row['workers']}")

# Daily trend
for r in conn.execute('''
    SELECT date(queried_at,'unixepoch','localtime') day, COUNT(*) n, ROUND(AVG(avg_score),3) avg
    FROM query_log WHERE queried_at > ? GROUP BY day ORDER BY day
''', (cutoff,)):
    print(f"  {r['day']}: {r['n']} queries, avg={r['avg']}")

# Last 15 queries — watch for generic titles
for q in conn.execute('''
    SELECT query_text, avg_score, top_score, top_result_title,
           datetime(queried_at,'unixepoch','localtime') t
    FROM query_log ORDER BY queried_at DESC LIMIT 15
'''):
    print(f"  [{q['t']}] score={q['avg_score']:.3f} top={q['top_score']:.3f} "
          f"best=\"{q['top_result_title'][:50]}\" q=\"{q['query_text'][:50]}\"")
```

### Interpreting the diagnostics

| Signal | Meaning | Fix |
|--------|---------|-----|
| avg_score < 0.7 | Retrieval itself is weak | Check index freshness, reindex, check embedding server |
| avg_score > 0.7 but experiments repeat | Workers aren't reading results | Improve task body instructions, add "read top result" step |
| top_result_title = "results" or "exp_NNN_results" | Indexed docs are generic filenames, not structured summaries | Fix `prepare_text_for_embedding()` in experiment_rag.py — extract hypothesis, not filename stem |
| Same thread dominates recent queries | Curiosity queue clustering | Check thread diversity cap (35% max), run curiosity_scorer.py |
| Queries drop to 0 for a day | Workers stopped using RAG | Check if task body instructions still include RAG search step |

### The retrieval-vs-downstream distinction

RAG scores measure whether *relevant past work exists and is found*.
They do NOT measure whether workers *act on that knowledge well*.

If scores are 0.8+ but experiments keep redesigning the same approaches,
the bottleneck is worker behavior (not reading results carefully enough,
or the curiosity queue pushing toward the same threads), not retrieval.

**Diagnostic flow:**
1. Check RAG scores → if high (>0.75), retrieval is fine
2. Check if experiments are duplicating past work → if yes, workers aren't absorbing results
3. Check top_result_title → if generic ("results", "exp_NNN_results"), workers can't tell what a result IS from the title alone
4. Check query clustering → if same topic dominates, queue diversity cap may need tightening

**Fix priority:** Task body instructions (tell workers to read the top result's
hypothesis and result, not just note "relevant work exists") > index quality
(structured titles) > retrieval tuning (rarely needed).

### Generic titles pitfall (June 2026) — FIXED

The `prepare_text_for_embedding()` function in experiment_rag.py extracts
structured fields from JSON experiment files (hypothesis, result, domain).
But for non-JSON files (the majority), it fell back to the filename stem
as the title. This produced indexed entries like:

- `results` (from `exp_2818_results.json`)
- `exp_2673` (from `exp_2673.md`)
- `exp_2442_script_distance_real_latest` (from a results file)

**Root cause:** `index_experiments()` stored `fpath.stem` as the title,
ignoring what `extract_json_fields()` extracted. Additionally, the
`documents` table had no UNIQUE constraint on `source_path`, so each
reindex created duplicate rows instead of updating existing ones.

**Fix (June 3 2026):**
1. Added `extract_title()` function that pulls meaningful titles from
   JSON (title/hypothesis fields), MD (first # heading), PY (docstring
   or first comment), TXT (first meaningful line)
2. Changed `index_experiments()` to use `extract_title(content, fpath)`
   instead of `fpath.stem`
3. Added UNIQUE INDEX on `documents(source_path)` to prevent duplicate
   rows on reindex
4. Deduplicated existing table (18,615 → 2,363 rows)
5. Added `--force` flag to reindex all files even if unchanged

**Before:** Workers saw "exp_2818_results" — had to open file to learn anything.
**After:** Workers see "Llama-4's 83.3% ceiling on DICT is caused by attentional redirection"

**Also fixed: preview extraction (June 3 2026).** The `content_preview` field
(first 200 chars shown in query results) was raw JSON for JSON files. Added
`extract_preview()` that produces structured summaries like
"Hypothesis: X. Result: Y." Workers can now triage results in 2 seconds
without opening files.

## Other Commands

```bash
# Full reindex (updates titles, use after code changes)
python3 ~/.hermes/scripts/experiment_rag.py index --force

# Incremental index (only new/changed files)
python3 ~/.hermes/scripts/experiment_rag.py index --incremental

# Retry previously failed files
python3 ~/.hermes/scripts/experiment_rag.py index --reindex-failed

# Status (document count, query quality stats)
python3 ~/.hermes/scripts/experiment_rag.py status

# Keep embedding server alive (usually handled by systemd)
python3 ~/.hermes/scripts/experiment_rag.py serve
```

## Quality Benchmarks (observed)

- Good avg_score: 0.75+ (retrieval is solid)
- Expected high-quality rate: >95% (queries returning relevant results)
- Low quality threshold: <0.4 (something is wrong with indexing or server)
- 25 unique workers actively querying (as of June 2026)
- ~1,600 queries/day at 32-worker scale
