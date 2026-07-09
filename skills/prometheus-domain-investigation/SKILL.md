---
name: prometheus-domain-investigation
description: Investigate and fix domain misclassification in the Prometheus evolutionary mechanism-discovery system. Covers the classification pipeline (result_bridge → write_worker_result → apply_worker_results → reclassify_domains cron), common failure modes, and backfill procedures.
---

# Prometheus Domain Investigation

## When to Use

- The `build_topology_export.py` report shows an unexpectedly large domain (especially "unknown")
- Experiments are accumulating in a domain that should have been reclassified
- The auto-classification pipeline appears to be running but not making progress
- A domain is growing faster than it should be

## Architecture: How Domain Classification Works

Prometheus uses a multi-layer domain classification pipeline:

1. **result_bridge.py** (10m cron) — bridges `kanban_complete` task results into `worker_results`. Sets `domain = ""` (was `"unknown"` before 2026-06-30 fix). Only overrides if the kanban task metadata JSON contains a `domain` field. Does NOT call any classifier itself.

2. **write_worker_result.py** (direct CLI path) — workers that call this directly get `auto_classify_domain()` which uses the embedding classifier (nomic on port 9150, threshold >0.3) with regex fallback.

3. **apply_worker_results.py** (2m cron) — processes worker_results, derives verdicts, and auto-classifies domains that are empty or in `JUNK_DOMAINS`. Uses embedding classifier first (threshold >=0.40), falls back to keyword matching. Also runs the domain_creation_gate to redirect fragmented domains to canonical parents.

4. **reclassify_domains.py** (15m cron, job ID `a121191f6afa`) — finds experiments in non-canonical domains (orphans) and reclassifies them using embedding similarity. Canonical = any domain with >=10 experiments OR in the embedding classifier's known set. The canonical set includes both hard-coded `DOMAIN_DESCRIPTIONS` and auto-generated descriptions from `domain_auto_descriptions.json`.

## Common Failure Modes

### Domain self-promotes to canonical by volume
A junk/placeholder domain (e.g., "unknown") accumulates experiments because it's the default value somewhere. Once it crosses 10 experiments, `reclassify_domains.py` treats it as canonical and stops reclassifying its experiments. Fix: explicitly `canonical.discard()` it in `get_database_canonical_domains()`.

### Auto-generated domains missing from canonical set
`domain_auto_descriptions.json` contains 200+ auto-generated domain centroids, but if `EMBEDDING_CANONICAL` only uses hard-coded `DOMAIN_DESCRIPTIONS` keys, experiments matching auto-generated domains are rejected as non-canonical. Fix: merge auto-generated descriptions into `EMBEDDING_CANONICAL`.

### Self-referential centroid trap
If a junk domain ("unknown") has a centroid in `domain_embeddings.json`, experiments matching that centroid keep getting assigned back to the junk domain. Fix: remove the centroid from both `domain_embeddings.json` and `domain_auto_descriptions.json`.

### Stale transfer_tracking edges keep a domain in the topology
After reclassifying experiments away from a junk domain, the domain can still appear in the topology export long tail. This is because `build_topology_export.py` reads edges from `transfer_tracking`, which has historical rows with `source_domain` or `target_domain` set to the old domain. These rows are never updated when experiments are reclassified. The node shows `experiment_count: 1` (or 0) but still has in_degree/out_degree from stale edges, keeping it connected to the graph. Check:
```sql
SELECT COUNT(*) FROM transfer_tracking WHERE source_domain = 'X' OR target_domain = 'X';
```
Also check `build_topology_export.py` line ~21 — the `_excluded_domains` set filters ghost nodes from the export. If the junk domain isn't in that set, it will still appear as a node even with 1 experiment.

### result_bridge creates junk domains
`result_bridge.py` sets a default domain before checking metadata. If the default is a non-empty string that isn't in `JUNK_DOMAINS`, it persists as a real domain. Fix: set default to `""` so downstream pipelines classify it.

## Investigation Procedure

1. **Run the topology export** to see domain sizes:
   ```
   cd ~/.hermes && python3 scripts/build_topology_export.py
   ```

2. **Check DB domain distribution**:
   ```sql
   SELECT domain, COUNT(*) FROM experiments GROUP BY domain ORDER BY COUNT(*) DESC LIMIT 20;
   SELECT domain, COUNT(*) FROM worker_results GROUP BY domain ORDER BY COUNT(*) DESC LIMIT 20;
   ```

3. **Check time distribution** — is the domain still growing?
   ```sql
   SELECT date(created_at, 'unixepoch'), COUNT(*) FROM experiments WHERE domain = 'X' GROUP BY 1 ORDER BY 1;
   ```

4. **Check if the reclassify cron is running**:
   ```
   ls -la ~/.hermes/cron/output/a121191f6afa/ | tail -5
   cat the latest output file
   ```
   Look for "0 experiments will be reclassified" — this means the domain is canonical and being skipped.

5. **Check canonical status**:
   ```python
   from reclassify_domains import get_database_canonical_domains, get_db
   conn = get_db()
   canonical, all_counts = get_database_canonical_domains(conn)
   print('X in canonical?', 'X' in canonical)
   ```

6. **Trace where the domain is being set** — grep for the domain string in scripts:
   ```
   search_files pattern='domain.*=.*X' path=~/.hermes/scripts target=content file_glob=*.py
   ```

7. **Check what the embedding classifier returns** for stuck experiments:
   ```python
   from embedding_domain_classifier import classify_embedding
   res = classify_embedding(text[:2000], '')
   ```

## Backfill Procedure

After fixing the root cause(s), run the backfill immediately (don't wait for cron):

```bash
cd ~/.hermes/scripts && python3 reclassify_domains.py --orphans-only
```

This processes all orphan experiments in one pass. For large backfills (5000+), it may take several minutes due to embedding API calls. Run multiple passes if needed — some experiments may match to domains that were themselves non-canonical in the first pass (e.g., auto-generated domains missing from `EMBEDDING_CANONICAL`). After the first pass, check what remains:
```sql
SELECT hypothesis, domain FROM experiments WHERE domain = 'X' LIMIT 10;
```
Then classify a sample with the embedding classifier to see what domain it's matching to and why it's being rejected. Common pattern: matches to an auto-generated domain that isn't in the canonical set — fix `EMBEDDING_CANONICAL` and re-run. Another pattern: matches to the junk domain itself because of a self-referential centroid — purge the centroid from `domain_embeddings.json` and `domain_auto_descriptions.json`, then re-run.

Verify before/after:
```sql
SELECT COUNT(*) FROM experiments WHERE domain = 'X';
```

## Diagnosing Ghost Edges in the Topology (Post-Reclassification)

After reclassifying experiments away from a junk domain, the user may report that "emerging topics" disappeared from the topology long tail and only the junk domain remains visible. This happens because `transfer_tracking` edges are historical and never updated when experiments change domains.

### Investigation steps

1. Check if the junk domain still appears as a topology node despite having ~1 experiment:
   ```python
   import json
   with open('~/.hermes/topology_full_export.json') as f:
       d = json.load(f)
   node = d['nodes'].get('unknown', {})
   print(f'experiment_count: {node.get("experiment_count")}')
   print(f'in_degree: {node.get("in_degree")}, out_degree: {node.get("out_degree")}')
   ```

2. Check stale edges in transfer_tracking:
   ```sql
   SELECT COUNT(*) FROM transfer_tracking WHERE source_domain = 'X' OR target_domain = 'X';
   -- Also check the distribution of edge volumes
   SELECT source_domain, target_domain, COUNT(*) as cnt
   FROM transfer_tracking WHERE source_domain = 'X' OR target_domain = 'X'
   GROUP BY source_domain, target_domain ORDER BY cnt DESC LIMIT 10;
   ```

3. Check flow dominance — how many of the top cross-domain flows involve the junk domain:
   ```python
   flows = d['cross_domain_flows']
   sorted_flows = sorted(flows, key=lambda x: x.get('transfer_count', 0), reverse=True)
   unknown_in_top20 = sum(1 for f in sorted_flows[:20]
                          if f['source'] == 'X' or f['target'] == 'X')
   ```

4. Check if the junk domain is in `build_topology_export.py`'s `_excluded_domains` set (line ~21). If not, it will still appear as a node even with 1 experiment. The current set is:
   ```python
   _excluded_domains = {'uncategorized', 'unclassified_pending', 'split_per_experiment', '', 'unknown'}
   ```
   "unknown" was added on 2026-06-30. Adding a junk domain here filters it from the export as defense-in-depth.

5. Check if newly reclassified domains have any edges (they likely won't — their transfer_tracking was recorded under the old domain):
   ```python
   for domain in ['motor_learning', 'chemical_kinetics', ...]:
       edges_in = sum(1 for f in flows if f['target'] == domain)
       edges_out = sum(1 for f in flows if f['source'] == domain)
   ```

### Why emerging topics disappear

Before reclassification, small domains had edges to the junk domain (which was massive). Those edges made them visible as emerging topics. After reclassification, the experiments moved to real domains, but the edges still point to the junk domain. The real emerging domains have hundreds of experiments but almost no edges, making them invisible in the flow-based topology. The junk domain's ghost edges dominate the "Top Cross-Domain Flows" table and crowd out real emerging domain pairs.

### Fix (applied 2026-06-30)

Two actions taken:

1. **Backfilled transfer_tracking**: Updated 1,238 resolvable rows — replaced "unknown" with the real domain via the experiment chain (399 source via `source_result_id`, 817 target via `destination_result_id`, 22 `completed_same_domain` using `source_domain`). The remaining ~1,481 rows are dead (`queued`/`abandoned`/`task_created`) and were left alone — no deletion.

2. **Added "unknown" to `_excluded_domains`** in `build_topology_export.py` (line 21) so the ~1,481 dead rows don't create ghost nodes/edges in future exports.

Result: topology rebuilt with 260 nodes, 0 "unknown", emerging topics (generative_models, drug_delivery, glycobiology, pomdp, etc.) visible in the long tail again.

See `references/transfer-tracking-ghost-edges.md` for the full resolution mechanics and SQL.
See `references/domains-registry-table.md` for the `domains` table — the last data source the dashboard reads that no backfill script touches.

## Post-Fix: Backfill All Tables That Carry Domain (Not Just experiments)

After reclassifying `experiments.domain`, the user may STILL see the junk domain in the live dashboard. This is because the dashboard (port 8888/8889) reads from `worker_results` and `knowledge_claims` directly, not from `experiments`. These tables have their own `domain` column that was never updated.

### Tables to backfill

1. **worker_results** — `domain` column, resolvable via `experiment_id -> experiments.domain`:
   ```sql
   UPDATE worker_results
   SET domain = (SELECT e.domain FROM experiments e WHERE e.id = worker_results.experiment_id)
   WHERE domain = 'unknown'
     AND EXISTS (SELECT 1 FROM experiments e WHERE e.id = worker_results.experiment_id
                 AND e.domain IS NOT NULL AND e.domain != 'unknown');
   ```
   Remaining rows with no matching experiment (e.g., `exp_bridge_*` IDs from BENCH3) need embedding classification from `key_finding` text.

2. **knowledge_claims** — `domain` column, resolvable via `first_experiment_id -> experiments.domain`:
   ```sql
   UPDATE knowledge_claims
   SET domain = (SELECT e.domain FROM experiments e WHERE e.id = knowledge_claims.first_experiment_id)
   WHERE domain = 'unknown'
     AND EXISTS (SELECT 1 FROM experiments e WHERE e.id = knowledge_claims.first_experiment_id
                 AND e.domain IS NOT NULL AND e.domain != 'unknown');
   ```

### Why the dashboard doesn't reflect experiment fixes

The topology export (`build_topology_export.py`) reads `experiments.domain` for node metadata, but the dashboard services (`prometheus-dashboard.service` port 8888, `prometheus-dashboard-v2.service` port 8889) query `worker_results`, `knowledge_claims`, AND the `domains` registry table directly at runtime. Fixing `experiments` alone fixes the topology but NOT the dashboard. ALL tables must be backfilled for the junk domain to disappear from every view.

### The `domains` registry table — the LAST thing to check but the one the dashboard actually reads

The dashboard v2 (`prometheus_dashboard_v2.py`) `get_domains()` function (line ~416) reads from a `domains` table in `prometheus.db`:

```sql
SELECT d.name, d.confidence, d.id,
  (SELECT COUNT(*) FROM experiments e WHERE e.domain = d.name) as exp_count,
  ...
FROM domains d ORDER BY exp_count DESC
```

This table is a registry of known domains — separate from `experiments`, `worker_results`, and `knowledge_claims`. Even after you backfill all three of those tables and zero out the junk domain everywhere, a single row in `domains` with `name = 'unknown'` will make the dashboard show it in the long tail (exp_count=0 but the row still exists, landing in the `ec < 2` long_tail tier).

PITFALL: You will backfill experiments, worker_results, knowledge_claims, transfer_tracking, rebuild the topology export, and the user will STILL see the junk domain in the dashboard. This is because the `domains` table is a separate registry that is never touched by any of the reclassification or backfill scripts. Check it last:

```sql
SELECT name, confidence FROM domains WHERE name = 'unknown';
```

If the row exists, it must be removed (or the domain renamed to something real if it had legitimate data):

```sql
DELETE FROM domains WHERE name = 'unknown';
```

The `domains` table has no foreign key constraints to experiments or worker_results — it is a standalone registry. Removing a junk entry does NOT affect any experiment, worker result, or knowledge claim. The dashboard will simply stop showing that domain as a tier.

### The domains table is supposed to be auto-rebuilt — check WHY it's stale before deleting manually

Before manually deleting rows from the `domains` table, check whether the auto-rebuild cron is working. The `domain_taxonomy_maintenance` cron (job `784eff56cd4a`, every 15m) is supposed to rebuild the `domains` table automatically. Its step 2 calls `normalize_all_domains.py` which:
1. Drops all rows from the `domains` table (`DELETE FROM domains`)
2. Rebuilds from `SELECT domain, COUNT(*) FROM experiments GROUP BY domain`
3. Inserts each domain with a confidence score based on experiment count

If this cron step is CRASHING, the `domains` table stays stale — it keeps old entries (like "unknown") and never picks up new domains from reclassification (like motor_learning, chemical_kinetics, automotive). The user will say "where the fuck are my other domains gonna come from" — they're in experiments but the domains registry was never rebuilt.

**Known crash**: `normalize_all_domains.py` line ~209 crashes with `sqlite3.IntegrityError: NOT NULL constraint failed: domains.name` when experiments has a NULL or empty string domain. The rebuild loop iterates over `SELECT domain, COUNT(*) FROM experiments GROUP BY domain` which includes NULL/empty rows. Fix (applied 2026-06-30): added `WHERE domain IS NOT NULL AND domain != ''` to the rebuild query so NULL/empty domains are skipped.

**PITFALL: The cron will resurrect a junk domain if any experiment still has it.** Even after fixing the crash and manually `DELETE FROM domains WHERE name = 'unknown'`, the 15-minute `domain_taxonomy_maintenance` cron rebuilds the `domains` table from `experiments`. If even ONE experiment still has `domain = 'unknown'`, the cron re-inserts the "unknown" row on the next tick. The user will report "it's back" and you'll look like an idiot. You MUST zero out `experiments.domain = 'unknown'` (reclassify or manually set the last few stragglers to a real domain) BEFORE deleting from the `domains` table. If you can't reclassify a straggler (e.g., it matches a deliberately-excluded methodology label like 'tfidf'), manually set it to `transfer_learning` or `general` — anything but the junk domain. Verify with `SELECT COUNT(*) FROM experiments WHERE domain = 'unknown'` returns 0 before declaring victory.

**How to check if the cron is crashing**:
```bash
# Find the latest output
ls -t ~/.hermes/cron/output/784eff56cd4a/ | head -1
# Read it — look for "normalize failed" or "WARN"
cat ~/.hermes/cron/output/784eff56cd4a/$(ls -t ~/.hermes/cron/output/784eff56cd4a/ | head -1)
```

If step 2 is failing, the domains table will be stale with the wrong domain count and missing emerging domains. Fix the crash in `normalize_all_domains.py`, then the next cron tick will auto-rebuild the table correctly — no manual DELETE needed.

PITFALL: You can backfill experiments, worker_results, knowledge_claims, transfer_tracking, rebuild the topology, and STILL see the junk domain in the dashboard because the `domains` registry table is stale. Check whether the auto-rebuild cron is crashing FIRST — if it is, fixing the crash will sync everything automatically.

## Post-Fix: Filter transfer_pairs in build_topology_export.py

Adding a domain to `_excluded_domains` in `build_topology_export.py` filters it from nodes and edges (line 33-36), but the `transfer_pairs` dict (line 77-81) is built separately and was NOT filtered. This means ghost flows still appear in the "Top Cross-Domain Flows" table in the HTML report even after the node is excluded. Fix: add the same exclusion check to the transfer_pairs filter:

```python
# Line 80 — was:
if src != tgt:
# Change to:
if src != tgt and src not in _excluded_domains and tgt not in _excluded_domains:
```

Without this, the dashboard/topology report will still show the junk domain in flow tables even though it's excluded as a node.

## Key Files

- `~/.hermes/scripts/result_bridge.py` — kanban bridge, sets default domain
- `~/.hermes/scripts/write_worker_result.py` — direct CLI path, auto_classify_domain()
- `~/.hermes/scripts/apply_worker_results.py` — cron apply step, JUNK_DOMAINS set
- `~/.hermes/scripts/reclassify_domains.py` — cron reclassify, canonical set logic
- `~/.hermes/scripts/embedding_domain_classifier.py` — DOMAIN_DESCRIPTIONS, embedding cache
- `~/.hermes/scripts/domain_classifier.py` — regex fallback classifier
- `~/.hermes/scripts/domain_creation_gate.py` — domain fragmentation guard
- `~/.hermes/domain_embeddings.json` — cached domain centroid embeddings
- `~/.hermes/domain_auto_descriptions.json` — auto-generated domain descriptions
- `~/.hermes/scripts/normalize_all_domains.py` — rebuilds the `domains` registry table from `experiments` (called by `domain_taxonomy_maintenance.py` step 2). Crashes on NULL/empty domain strings.
- `~/.hermes/scripts/domain_taxonomy_maintenance.py` — 6-step cron pipeline that merges, normalizes, classifies, updates confidence, prunes, and checks for unmapped domains. Job ID `784eff56cd4a`, every 15m.
- `~/.hermes/scripts/build_topology_export.py` — builds topology JSON + HTML report. `_excluded_domains` set filters ghost nodes/edges. `transfer_pairs` must also be filtered (line ~80).
- `~/.hermes/prometheus.db` — SQLite database (experiments, worker_results, knowledge_claims, domains, transfer_tracking)
- `~/.hermes/prometheus.db` `domains` table — standalone domain registry read by the dashboard v2 `get_domains()` function. NOT updated by any reclassification or backfill script. Must be checked and cleaned manually.
- `~/.hermes/cron/jobs.json` — cron job definitions (54 jobs)

## User Preferences for This Task Class

- The user wants investigation BEFORE changes. Do a full root-cause analysis and present findings before touching code.
- Once the user approves fixes, execute immediately. Do NOT wait for scheduled cron cycles — run backfills manually.
- Report results with concrete numbers (before/after counts).
- Update `~/.hermes/docs/architecture-changelog.md` after significant changes. Add to the TOP only, never delete existing entries.
- KEEP IT SIMPLE. When explaining complex investigation results, lead with a one-sentence bottom line, not the full analysis. The user will ask for detail if they want it. Do not dump multi-paragraph breakdowns of row counts and join chains when asked "what needs to be done" — give the action and the risk, nothing else.
- Do NOT propose deleting data without checking what reads it first. The user will ask "won't deleting shit fuck the system up" — have the answer ready before proposing it. Check all consumers (cron scripts, topology builder, health snapshot, dashboard services) before recommending deletion.
- When the user asks "what needs to be done" or "what can be done", give 2-3 bullet points max. Do NOT dump row counts, join chains, and resolution breakdowns. The user will say "what the fuck is all this simplify the result" — they want the action and the risk, nothing else. If they want detail they'll ask.
- After fixing the primary data source (experiments), remember to check ALL tables and ALL consumers. The user will come back saying "still shows unknown in my dashboard" because worker_results and knowledge_claims weren't backfilled. Always backfill every table that carries a domain column, not just the one you started with.
- When the user says "still shows unknown" after you've made fixes, STOP GUESSING and go find what the dashboard ACTUALLY queries. Read the dashboard source code (`prometheus_dashboard_v2.py`), find the `get_domains()` function, trace what table it reads from. Do not assume — go look at the actual query. The user will say "what the fuck are you actually doing" if you make another arbitrary fix that doesn't target the real data source.
- The investigation order for "domain still showing somewhere": (1) experiments table, (2) worker_results, (3) knowledge_claims, (4) transfer_tracking, (5) domains registry table, (6) topology export, (7) dashboard source code. Check ALL of them before declaring victory. The user's frustration compounds with each round of "fixed it" / "no it's still there" — get it right the first time by checking every consumer.
- When the user asks "where are my other domains gonna come from" — they're pointing out that the auto-rebuild mechanism is broken. Don't just manually fix the data; find out WHY the auto-rebuild isn't working. The `domains` table is supposed to be rebuilt by `normalize_all_domains.py` via the `domain_taxonomy_maintenance` cron. If that cron is crashing, the domains registry stays stale and no new domains appear. Check the cron output before proposing manual fixes.