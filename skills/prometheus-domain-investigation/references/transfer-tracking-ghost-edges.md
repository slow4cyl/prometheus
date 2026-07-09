# Transfer Tracking Ghost Edges — Session 2026-06-30

## Context

After purging 6,345 experiments from the "unknown" domain (3-pass backfill), the user reported that "emerging topics" disappeared from the topology long tail — only "unknown" was visible. Investigation confirmed stale `transfer_tracking` edges were the cause. Fix was applied same session.

## Root Cause

`transfer_tracking` in `prometheus.db` records historical transfer events with `source_domain` and `target_domain` columns. When 6,346 experiments were in "unknown", every transfer to/from those experiments was recorded with `unknown` as the domain. After reclassifying the experiments (changing `experiments.domain`), `transfer_tracking` was NOT updated. The 2,721 transfer records still pointed to "unknown".

## Key Numbers

- `transfer_tracking` rows involving "unknown": 2,721 (399 as source, 2,322 as target)
- Top ghost flows: cross_domain_prediction->unknown (305), network_science->unknown (295), machine_learning->unknown (193), statistics->unknown (168)
- "unknown" in topology: 1 experiment, 33 in_degree, 78 out_degree, 209 total edges
- 7 of the top 20 cross-domain flows involved "unknown" (19.7% of total flow volume)
- 168 partner domains had edges to "unknown" that were ghost connections

## Why Emerging Topics Vanished

Before the purge, small domains (motor_learning, chemical_kinetics, automotive) had edges going to "unknown" — a massive node with 6,346 experiments. Those edges made them visible as "emerging topics" in the topology graph and flow tables.

After the purge, those experiments moved to real domains (motor_learning: 601, chemical_kinetics: 438, automotive: 166), but the edges still said "unknown". So:
- motor_learning had 601 experiments but only 7 edges (3 in, 4 out)
- chemical_kinetics had 438 experiments but only 2 edges
- biodiversity had 56 experiments and 0 edges
- "unknown" had 1 experiment but 209 edges with 2,721 transfers

## Fix Applied (2026-06-30)

### 1. Backfilled transfer_tracking (1,238 rows updated)

Join chain: `transfer_tracking.source_result_id -> worker_results.id -> experiments.domain`
and: `transfer_tracking.destination_result_id -> worker_results.id -> experiments.domain`

Resolvability breakdown:
- source_domain='unknown': 399 rows, all resolvable via source_result_id -> experiments.domain
- target_domain='unknown': 2,322 rows:
  - 817 resolvable via destination_result_id -> experiments.domain
  - 22 resolvable via completed_same_domain status (target = source)
  - 2 resolvable via destination_domain column
  - 1,481 unresolvable (queued/abandoned/task_created — never completed, left alone, no deletion)

Resolution SQL:
```sql
-- Resolve source_domain
UPDATE transfer_tracking
SET source_domain = (
  SELECT e.domain FROM worker_results wr
  JOIN experiments e ON wr.experiment_id = e.id
  WHERE wr.id = transfer_tracking.source_result_id
)
WHERE source_domain = 'unknown'
  AND EXISTS (
    SELECT 1 FROM worker_results wr
    JOIN experiments e ON wr.experiment_id = e.id
    WHERE wr.id = transfer_tracking.source_result_id
      AND e.domain IS NOT NULL AND e.domain != 'unknown'
  );

-- Resolve target_domain via destination_result_id
UPDATE transfer_tracking
SET target_domain = (
  SELECT e.domain FROM worker_results wr
  JOIN experiments e ON wr.experiment_id = e.id
  WHERE wr.id = transfer_tracking.destination_result_id
)
WHERE target_domain = 'unknown'
  AND destination_result_id IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM worker_results wr
    JOIN experiments e ON wr.experiment_id = e.id
    WHERE wr.id = transfer_tracking.destination_result_id
      AND e.domain IS NOT NULL AND e.domain != 'unknown'
  );

-- Resolve completed_same_domain rows (target = source)
UPDATE transfer_tracking
SET target_domain = source_domain
WHERE target_domain = 'unknown'
  AND status = 'completed_same_domain'
  AND source_domain IS NOT NULL
  AND source_domain != 'unknown';
```

### 2. Added "unknown" to _excluded_domains in build_topology_export.py

Two places needed the filter:

```python
# Line 21 — node exclusion:
_excluded_domains = {'uncategorized', 'unclassified_pending', 'split_per_experiment', '', 'unknown'}

# Line 80 — transfer_pairs flow exclusion (was missing, caused ghost flows in HTML report):
if src != tgt and src not in _excluded_domains and tgt not in _excluded_domains:
```

The node exclusion (line 21) filters ghost nodes from the export. But `transfer_pairs` (line 77-81) is built separately and was NOT filtered — ghost flows still appeared in the "Top Cross-Domain Flows" table in the HTML report even after the node was excluded. Both filters are needed.

### 3. Backfilled worker_results and knowledge_claims domain columns

After fixing `experiments.domain` and `transfer_tracking`, the user reported "still shows unknown in my dashboard". The live dashboard (ports 8888/8889) reads from `worker_results` and `knowledge_claims` directly, not from `experiments`. These tables had their own stale `domain = 'unknown'` values:

- `worker_results`: 6,440 rows with domain='unknown', 6,439 resolvable via `experiment_id -> experiments.domain`. Remaining 216 were BENCH3 bridge experiments with no experiment record — classified via embedding classifier from `key_finding` text.
- `knowledge_claims`: 1,347 rows with domain='unknown', all resolvable via `first_experiment_id -> experiments.domain`.

Resolution SQL:
```sql
-- worker_results
UPDATE worker_results
SET domain = (SELECT e.domain FROM experiments e WHERE e.id = worker_results.experiment_id)
WHERE domain = 'unknown'
  AND EXISTS (SELECT 1 FROM experiments e WHERE e.id = worker_results.experiment_id
              AND e.domain IS NOT NULL AND e.domain != 'unknown');

-- knowledge_claims
UPDATE knowledge_claims
SET domain = (SELECT e.domain FROM experiments e WHERE e.id = knowledge_claims.first_experiment_id)
WHERE domain = 'unknown'
  AND EXISTS (SELECT 1 FROM experiments e WHERE e.id = knowledge_claims.first_experiment_id
              AND e.domain IS NOT NULL AND e.domain != 'unknown');
```

For the 216 bridge rows with no experiment record, ran embedding classification from `key_finding` text (same as the reclassify pipeline uses).

### Result

Topology rebuilt: 260 nodes, 0 "unknown", emerging topics visible in long tail (generative_models: 3, computing_substrates: 4, drug_delivery: 4, arithmetic: 8, cnc_machining: 8, etc.).

## Key Lesson: Don't Delete Without Checking Consumers

The user challenged the deletion proposal ("won't deleting shit fuck the system up"). Before proposing deletion, checked all consumers:
- `build_topology_export.py` line 78: reads ALL transfer_tracking rows regardless of status (no WHERE status filter) — dead rows contribute ghost edges
- `health_snapshot.py`: only counts `completed%` rows — dead rows don't affect health metrics
- `transfer_tracking.py` stats: only counts completed rows
- `janitor_orphaned_transfer_tracking.py`: designed to mark old queued rows as abandoned — the system already has a mechanism for dead rows

Conclusion: deletion was unnecessary. The dead rows don't affect health metrics, and adding "unknown" to `_excluded_domains` filters them from the topology. The 1,238 resolvable rows were updated, not deleted.

## Verification Queries

```sql
-- How many stale edges remain?
SELECT COUNT(*) FROM transfer_tracking WHERE source_domain = 'unknown' OR target_domain = 'unknown';

-- Which partner domains are affected?
SELECT source_domain, target_domain, COUNT(*) as cnt
FROM transfer_tracking WHERE source_domain = 'unknown' OR target_domain = 'unknown'
GROUP BY source_domain, target_domain ORDER BY cnt DESC LIMIT 20;

-- Do reclassified domains have edges?
SELECT 'motor_learning' as domain,
  (SELECT COUNT(*) FROM transfer_tracking WHERE source_domain = 'motor_learning') as src_edges,
  (SELECT COUNT(*) FROM transfer_tracking WHERE target_domain = 'motor_learning') as tgt_edges;
```
