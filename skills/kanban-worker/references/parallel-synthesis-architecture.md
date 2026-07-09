# Parallel Synthesis Architecture (June 2026)

## Problem

The synthesis worker was a single-threaded bottleneck. It did TWO jobs:
1. **Result collection** — extracting experiment findings from kanban task summaries
2. **Cross-experiment analysis** — patterns, queue curation, domain confidence

With 1 synthesis worker, only 1 synthesis task could run at a time. 4+ tasks
queued up while 22 worker slots sat idle.

Additionally, only 20% of experiment workers called write_worker_result.py,
so synthesis had to manually extract results for 80% of experiments.

## Solution

Split the two jobs:

**Result collection → Worker Results Pipeline (existing)**
- Experiment workers write structured results via write_worker_result.py
- Results go to worker_results table in prometheus.db
- apply_worker_results.py (cron 2m) merges to experiments table + self_state.json
- batch_create_tasks.py now MANDATES write_worker_result.py in task bodies
- This closes the 80% gap — workers write their own results

**Cross-experiment analysis → Parallel Synthesis via synthesis_outputs table**
- Multiple synthesis workers can run concurrently
- Each writes to synthesis_outputs table (own row, no conflicts)
- synthesis_merger.py (cron 2m) reads unapplied rows
- Merges to self_state.json atomically (single writer)
- Handles: queue resolutions, new curiosities, counter reconciliation

## Architecture

```
EXPERIMENT WORKER                    SYNTHESIS WORKER(s)
      |                                    |
  finishes exp                          finishes analysis
      |                                    |
  calls write_worker_result.py       calls write_synthesis_output.py
  → worker_results TABLE             → synthesis_outputs TABLE
      |                                    |
      v                                    v
 apply_worker_results.py            synthesis_merger.py
 (cron 2m)                          (cron 2m)
 merges to experiments table        merges to self_state.json
 + self_state.json                  (single writer, atomic)
      |                                    |
      +------------ BOTH WRITE TO ---------+
                   prometheus.db (SQLite WAL)
```

## Scripts

- `~/.hermes/scripts/write_synthesis_output.py` — synthesis workers call this
- `~/.hermes/scripts/synthesis_merger.py` — cron applies outputs to self_state.json
- `~/.hermes/scripts/write_worker_result.py` — experiment workers call this (existing)
- `~/.hermes/scripts/apply_worker_results.py` — cron applies worker results (existing)

## Database Schema

```sql
CREATE TABLE synthesis_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    synthesis_task_id TEXT NOT NULL,
    worker_id TEXT DEFAULT 'prometheus-synthesis',
    experiments_covered TEXT,      -- JSON array of exp IDs
    queue_resolutions TEXT,        -- JSON array of {item, resolved_by, note}
    new_curiosities TEXT,          -- JSON array of new queue items
    counter_overrides TEXT,        -- JSON dict of counter overrides
    domain_confidence_updates TEXT, -- JSON dict of {domain: confidence}
    key_patterns TEXT,             -- Free text: cross-experiment patterns
    created_at INTEGER DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
    applied INTEGER DEFAULT 0     -- 1=merged to self_state.json
);
```

## Why This Works

1. **No race conditions** — Each synthesis worker writes to its own row in SQLite.
   SQLite WAL handles concurrent writes safely.
2. **Single writer for self_state.json** — The merger script is the ONLY thing
   that writes to self_state.json. It reads all unapplied rows and applies them
   in order. No concurrent writes, no corruption.
3. **Parallel synthesis** — Multiple synthesis tasks can run simultaneously.
   The Director can assign synthesis to any free worker, not just prometheus-synthesis.
4. **Faster result flow** — Experiment workers write results directly (no waiting
   for synthesis). Synthesis focuses on higher-level analysis.
5. **Idempotent merger** — The merger checks `applied=0` before processing.
   Re-running is safe. Partial failures don't corrupt state.

## Cross-Pollination (July 2026)

The synthesis worker now generates cross-domain transfer questions, not just
same-thread follow-ups. This breaks the "drilling deeper on one topic" trap.

**How it works:**
1. Experiment workers write findings with MECHANISM (WHY), not just verdict
2. Synthesis task body includes WHY IT WORKS for each experiment
3. Synthesis worker asks: "What mechanism? Where else? What do you want next?"
4. [TRANSFER] questions go to queue with +15 bonus in curiosity scorer
5. Director picks up high-score transfer items → experiments in new domains

**Key script:** `~/.hermes/scripts/generate_synthesis_body.py`
- Reads experiment findings from worker_results table (includes mechanisms)
- Generates complete synthesis task body with WHY IT WORKS + CROSS-POLLINATION
- Director uses this instead of manually constructing task bodies

**Why helper scripts matter:** LLMs don't reliably fill templates with specific
data from databases. The Director prompt said "read from worker_results table"
but the LLM just copied the template literally. Building a script that queries
the DB and generates the correct output is more reliable than relying on LLM
to execute the query. This pattern applies whenever an LLM needs to incorporate
specific data into a structured output.

## Migration Notes

- Old synthesis tasks that write to self_state.json directly will still work
  (the merger just won't find their outputs in the table). This is fine —
  the old path is a fallback.
- New synthesis tasks should use write_synthesis_output.py. The Director's
  cron prompt has been updated to instruct this.
- The synthesis_outputs table is created automatically by write_synthesis_output.py
  if it doesn't exist (idempotent CREATE TABLE IF NOT EXISTS).
