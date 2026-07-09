# The `domains` Registry Table — The Last Data Source

## Context

After backfilling experiments, worker_results, knowledge_claims, AND transfer_tracking — and rebuilding the topology export — the user STILL saw "unknown" in the dashboard long tail. The root cause was a `domains` table in `prometheus.db` that none of the backfill scripts touch.

## What the `domains` Table Is

A standalone registry of known domains in `prometheus.db`:

```sql
CREATE TABLE domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    confidence REAL DEFAULT 0.5,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
```

No foreign keys. No triggers. The dashboard reads it directly to build the mastery/emerging/long_tail tiers.

## How the Dashboard Uses It

`prometheus_dashboard_v2.py` `get_domains()` (line ~416):

```python
domains = db.execute("""
    SELECT d.name, d.confidence, d.id,
      (SELECT COUNT(*) FROM experiments e WHERE e.domain = d.name) as exp_count,
      ...
    FROM domains d ORDER BY exp_count DESC, d.confidence DESC
""").fetchall()
tiers = {'mastery': [], 'emerging': [], 'long_tail': []}
for d in domains:
    ec = d['exp_count'] or 0
    if ec >= 5:
        tiers['mastery'].append(entry)
    elif ec >= 2:
        tiers['emerging'].append(entry)
    else:
        tiers['long_tail'].append(entry)
```

A domain with exp_count=0 still appears in `tiers['long_tail']` because the `domains` row exists. The dashboard renders the long tail as a list of domain names — so "unknown" shows up even with zero experiments.

## The Auto-Rebuild Pipeline

The `domains` table is supposed to be rebuilt automatically by the `domain_taxonomy_maintenance` cron (job `784eff56cd4a`, every 15m). Its step 2 calls `normalize_all_domains.py` which:

1. Drops all rows from the `domains` table (`DELETE FROM domains`)
2. Rebuilds from `SELECT domain, COUNT(*) FROM experiments GROUP BY domain`
3. Inserts each domain with a confidence score based on experiment count

### Known Crash (Fixed 2026-06-30)

`normalize_all_domains.py` line ~209 crashed with `sqlite3.IntegrityError: NOT NULL constraint failed: domains.name` because experiments had NULL/empty domain strings. The `GROUP BY domain` query included NULL/empty rows, and the INSERT loop tried to insert them, hitting the NOT NULL constraint.

Fix: Added `WHERE domain IS NOT NULL AND domain != ''` to the rebuild query.

When this cron step was crashing, the `domains` table stayed stale — it kept old entries (like "unknown") and never picked up new domains from reclassification (motor_learning, chemical_kinetics, automotive, etc. — 14 domains with 604+ experiments were invisible to the dashboard).

### PITFALL: The Cron Will Resurrect a Junk Domain

Even after fixing the crash and manually `DELETE FROM domains WHERE name = 'unknown'`, the 15-minute cron rebuilds the `domains` table from `experiments`. If even ONE experiment still has `domain = 'unknown'`, the cron re-inserts the "unknown" row on the next tick.

Resolution sequence (in order):
1. Fix the `normalize_all_domains.py` crash (so the cron can rebuild)
2. Zero out ALL `experiments.domain = 'unknown'` (reclassify the last few stragglers; if one can't be classified, manually set it to `transfer_learning` or `general`)
3. Verify: `SELECT COUNT(*) FROM experiments WHERE domain = 'unknown'` returns 0
4. Run `python3 scripts/normalize_all_domains.py` manually (or wait for cron tick)
5. Verify: `SELECT COUNT(*) FROM domains WHERE name = 'unknown'` returns 0

Do NOT skip step 2 — if you `DELETE FROM domains` while experiments still has the junk domain, the cron will re-add it within 15 minutes.

## Investigation

```sql
-- Does the junk domain have a row in the domains registry?
SELECT name, confidence FROM domains WHERE name = 'unknown';

-- Are emerging domains missing from the registry?
SELECT e.domain, COUNT(*) FROM experiments e
LEFT JOIN domains d ON d.name = e.domain
WHERE d.name IS NULL AND e.domain IS NOT NULL AND e.domain != ''
GROUP BY e.domain ORDER BY COUNT(*) DESC;

-- Is the auto-rebuild cron crashing?
ls -t ~/.hermes/cron/output/784eff56cd4a/ | head -1
cat ~/.hermes/cron/output/784eff56cd4a/$(ls -t ~/.hermes/cron/output/784eff56cd4a/ | head -1)
# Look for "normalize failed" or "WARN" in the output
```

## Why This Was the Hardest Bug to Find

Every other table (experiments, worker_results, knowledge_claims, transfer_tracking) has a clear lineage: domain is set somewhere in the pipeline, and you can trace it to a script. The `domains` table is a registry that exists outside the classification pipeline. No reclassify or backfill script touches it. It was only discovered when the user reported "still shows unknown" after ALL other fixes were applied and verified.

Furthermore, the `domains` table has its own auto-rebuild mechanism that can be silently crashing, making it look like manual fixes don't stick. The investigation chain is: (1) is the junk domain in the `domains` table? (2) is the auto-rebuild cron crashing? (3) are there still experiments with the junk domain that the cron will resurrect?

## Lesson

When investigating domain misclassification, enumerate ALL tables in `prometheus.db` that carry domain data. The `domains` table is easy to miss because it's not in the classification pipeline — it's a registry. But the dashboard reads it directly. Always check it last, and always check whether the auto-rebuild cron is working before proposing manual fixes.
