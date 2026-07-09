# Worker Results Queue Architecture

## Overview

Workers write structured experiment results to a `worker_results` table in
`prometheus.db`. A cron job applies them atomically to the experiments table
and `self_state.json` every 2 minutes.

This replaces the synthesis bottleneck for basic state updates. Workers no
longer need to wait for the synthesis worker to process their results.

## Scripts

- `~/.hermes/scripts/write_worker_result.py` — worker calls this
- `~/.hermes/scripts/apply_worker_results.py` — cron applies queue (every 2 min)

## Write a Result (worker side)

### CLI

```bash
python3 ~/.hermes/scripts/write_worker_result.py \
  --experiment exp_NNN \
  --finding "CONFIRMED: F1=0.95 BECAUSE adversarial inputs cluster in low-dim subspace" \
  --supported \
  --confidence 0.85 \
  --domain injection_detection \
  --tags CONFIRMED,surprise \
  --files "exp_NNN.py,exp_NNN_results.json" \
  --queue "Follow-up question 1?;Follow-up question 2?"
```

### Python import

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from write_worker_result import write_result

write_result(
    "exp_NNN",
    finding="HYPOTHESIS SUPPORTED: F1=0.95",
    supported=True,
    confidence=0.85,
    domain="injection_detection",
    tags=["CONFIRMED"],
    files=["exp_NNN.py"],
    queue=["Does this hold for non-Latin scripts?"]
)
```

## What Gets Updated (cron side)

1. `prometheus.db` experiments table — result, tags, domain
2. `self_state.json`:
   - `experiments_completed_list` (list of ID strings)
   - All count fields (experiments_completed, total, metrics, counters)
   - `curiosity_queue` (new items from `--queue` flag)

## self_state.json Structure

- `experiments` is an INTEGER (count), not a dict
- `experiments_completed_list` is a list of strings ("exp_119")
- `curiosity_queue` is list of dicts with "text" and "priority"
- All count fields must be updated in sync

## Rules

- `--finding`: one sentence with mechanism — what happened AND WHY (e.g., "CONFIRMED: F1=0.95 BECAUSE...")
  - Mechanism explanations are critical: they enable cross-domain transfer of insights
  - Bad: "F1=0.95 on injection detection" (verdict only)
  - Good: "F1=0.95 BECAUSE adversarial inputs cluster in low-dim subspace" (verdict + mechanism)
- `--supported` / `--refuted` — flag must match finding text
  (finding text is source of truth; flag is fallback only)
- `--queue`: semicolon-separated new curiosity items
  - Prefix with [TRANSFER] to mark cross-domain transfer questions
  - Example: `--queue "[TRANSFER] Does clustering pattern apply to RAG poisoning?;What about non-Latin scripts?"`
  - [TRANSFER] items get +15 scoring bonus in curiosity_scorer.py
- Old kanban task result field still works, but structured results preferred
- Applied entries get `applied=1` in worker_results table

## Enforcement — The 80% Gap

**Pitfall (June 2026):** The experiment task body template (references/experiment-task-body-template.md) includes a write_worker_result enforcement block, but `batch_create_tasks.py`'s `generate_task_body()` did NOT include it. Since the batch creator generates ~80% of all task bodies, only 20% of workers called write_worker_result.py. The other 80% relied on synthesis to manually extract results — creating a massive backlog.

**Fix:** The enforcement block was added to `generate_task_body()` in batch_create_tasks.py (June 2026). Every auto-generated experiment task body now ends with the MANDATORY result writing instruction.

**Verification:** Check that new task bodies contain "RESULT WRITING (MANDATORY":
```bash
# Create a dry-run task and check its body
python3 ~/.hermes/scripts/batch_create_tasks.py --dry-run --count 1 | grep -c "RESULT WRITING"
# Should return 1
```

**If enforcement is missing:** The batch_create_tasks.py generate_task_body() function needs the enforcement block. See the function around line 140 in the script. The block should appear after the EXPECTED line and before the closing `"""`.

## Backfill (one-time)

`backfill_worker_results.py` reads existing `experiments/exp_*.json` files,
extracts verdict/hypothesis/tags, and populates worker_results for experiments
that predate the structured results pipeline. Run once after deploying the
queue; safe to re-run (skips existing entries).

```bash
python3 ~/.hermes/scripts/backfill_worker_results.py
python3 ~/.hermes/scripts/apply_worker_results.py
```

Backfilled 318 experiments on 2026-06-03 (first deployment).

**Pitfall — verdict-only hypotheses polluting RAG (June 2026):** Some experiment JSON files have empty or verdict-only hypothesis fields ("REFUTED", "SUPPORTED"). The backfill script wrote these as-is into the experiments table. The RAG indexer picked them up as document titles — 185 garbage entries polluting the index. Worker queries against them got low-quality matches, dropping RAG quality. **Fix:** The backfill script now validates hypotheses are >30 chars and not just verdict words before writing. If the hypothesis is garbage, it tries to extract the real one from the kanban task title. Manual fix for existing data: update experiments table hypotheses, then re-index RAG: `python3 ~/.hermes/scripts/experiment_rag.py index --incremental`.

**Pitfall — verdict-only hypotheses (June 2026):** Some experiment JSON files have empty or verdict-only hypothesis fields ("REFUTED", "SUPPORTED"). The backfill script validates hypotheses are >30 chars and not just verdict words before writing. If the hypothesis is garbage, it tries to extract the real one from the kanban task title. Run the manual fix script if needed:

```bash
# Fix garbage hypotheses in experiments table
python3 -c "
import sqlite3, subprocess, json, re
db = sqlite3.connect('prometheus.db')
c = db.cursor()
c.execute('SELECT id, hypothesis FROM experiments WHERE length(hypothesis) < 30 AND hypothesis != \"\"')
for eid, hyp in c.fetchall():
    # ... fix logic
"
# Then re-index RAG
python3 ~/.hermes/scripts/experiment_rag.py index --incremental
```
