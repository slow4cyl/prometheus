# Cross-DB Reconciliation + Provenance Pattern

Use when a Prometheus monitor claims a completed Kanban task never reached Prometheus, but the task may legitimately land outside `worker_results` / `experiments`.

## Root-cause pattern

Title heuristics like `experiment` / `hypothesis` can misclassify synthesis or other non-experiment task classes as ordinary experiment tasks. The fix is not title exclusion; it is row-based coverage in the authoritative output table for that task class.

Example: synthesis tasks write `prometheus.db.synthesis_outputs.synthesis_task_id`, then `synthesis_merger.py` merges them into curiosities. A completed synthesis task with a real `synthesis_outputs` row is covered even if it has no `worker_results` or `experiments` row.

## Safe monitor fix

Add a coverage set from the task-class output table, e.g.:

```python
synthesis_task_ids = {
    r[0] for r in p.execute(
        "SELECT DISTINCT synthesis_task_id FROM synthesis_outputs "
        "WHERE synthesis_task_id IS NOT NULL AND synthesis_task_id!=''"
    )
}
```

Then skip/report as info before adding the task to `missing_kanban_results`:

```python
if t["id"] in synthesis_task_ids:
    covered_synthesis_outputs.append(t["id"])
    continue
```

Do NOT suppress by title (`'synthesis' in title`): that hides tasks that completed but never wrote their output row.

## Do not hide real failures

Whenever adding a new coverage path, add a separate alert for the corresponding stalled state. For synthesis:

```sql
SELECT id, synthesis_task_id, created_at
FROM synthesis_outputs
WHERE COALESCE(applied, 0)=0
  AND typeof(created_at) IN ('real','integer')
  AND created_at < now - 24h
```

This clears false loss alerts while still catching merger failures.

## Forward provenance ledger

If the output table generates downstream rows, add a ledger table populated at the exact downstream insert:

```sql
CREATE TABLE IF NOT EXISTS synthesis_curiosity_links (
    synthesis_output_id INTEGER NOT NULL,
    synthesis_task_id TEXT NOT NULL,
    curiosity_id INTEGER NOT NULL,
    method TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (synthesis_output_id, curiosity_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_synthesis_curiosity_links_curiosity
    ON synthesis_curiosity_links(curiosity_id);
```

Populate immediately after inserting the downstream curiosity using `last_insert_rowid()` or cursor `lastrowid` if reliably available in that DB wrapper.

## Backfill rule

Backfill only high-confidence links:

1. Parse historical output proposals.
2. Mirror the live cap/filter rules where possible.
3. Exact-match proposal text against downstream row text.
4. Restrict to a creation-time window.
5. Write only if the match is unique and unclaimed.
6. Report ambiguous/no-match/repaired/dedup-skipped cases; do not guess.

A partial forensic ledger is better than corrupt provenance.

## Verification floor

For live Prometheus scripts with no canonical test suite, create an isolated `/tmp/hermes-verify-*` script using temp `HOME` + fixture SQLite DBs. Verify both:

- the false-positive path clears, and
- the real failure path still alerts.

Clean up the verifier and label the result as ad-hoc targeted verification, not suite green.
