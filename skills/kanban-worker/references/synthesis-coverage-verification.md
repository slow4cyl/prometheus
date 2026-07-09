# Synthesis Coverage Verification — Don't Trust Titles OR Summaries

## The Problem

Synthesis tasks can claim to have consolidated experiments in BOTH their title AND their `latest_summary`, while `self_state.json`'s `experiments.completed` array does NOT contain those experiments. This was observed in Cycle #214:

```
Synthesis task t_ecdbb6b1:
  Title: "SYNTHESIS: consolidate exp_730, exp_731, exp_732, exp_733"
  Summary: "Consolidated exp_733 (auto-calibration...)"
  
self_state.json:
  exp_733 NOT in experiments.completed
  exp_730, exp_731, exp_732 also NOT in experiments.completed
```

The synthesis worker either failed to write, wrote to a stale version, or the write was incomplete. The summary is authoritative for what the worker ATTEMPTED, not what actually landed in self_state.json.

## Detection: Method A (Ground Truth)

The ONLY reliable detection method compares done task IDs directly against `self_state.json`:

```python
import json, re, os

# Extract experiment IDs from done task titles (exclude synthesis tasks)
done = json.load(open('/tmp/kanban_done.json'))
done_exp_ids = set()
for t in done:
    title = t.get('title', '')
    is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
    if not is_synth:
        for m in re.finditer(r'exp_(\d+\w*)', title):
            done_exp_ids.add(f'exp_{m.group(1)}')

# Ground truth: self_state.json experiments.completed
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if eid: ss_exp_ids.add(eid)

# Unsynthesized = done but not in self_state
unsynth = done_exp_ids - ss_exp_ids
```

This catches ALL variants:
- Synthesis title lists experiments but summary only covers a subset
- Synthesis summary claims coverage but self_state wasn't updated
- Synthesis worker crashed after writing summary but before self_state update

## What NOT to Trust

| Source | Reliable? | Why |
|--------|-----------|-----|
| `self_state.json` experiments.completed | ✅ YES | Ground truth — if it's here, it's synthesized |
| Done task title mentioning exp_NNN | ⚠️ Partial | Task completed but may not be in self_state yet |
| Synthesis task title listing experiments | ❌ NO | May list experiments the synthesis didn't actually cover |
| Synthesis task `latest_summary` claiming consolidation | ❌ NO | May claim coverage that didn't land in self_state |

## Post-Synthesis Verification (MUST RUN)

After a synthesis task completes (status=done), the Director MUST re-run Method A to verify the synthesis actually wrote to self_state.json. This catches "silent write failures" where the synthesis worker reports success but the write never landed.

**Observed in Cycle #229:** Synthesis task t_52c2a818 completed with summary "3 new experiments consolidated into self_state.json (915 total)" — but verification showed 0 of the claimed experiments in self_state.json, and metrics counter (915) drifted from array length (905) by 10.

```python
# Post-synthesis verification — run after synthesis task shows status=done
import json, re, os

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if eid: ss_exp_ids.add(eid)

array_len = len(ss_exp_ids)
metrics_count = ss.get('metrics', {}).get('experiments_completed', 0)
drift = array_len - metrics_count

if drift != 0:
    print(f"COUNTER DRIFT: array={array_len}, metrics={metrics_count}, drift={drift}")
    # Flag for reconciliation in next synthesis task

# Check if synthesis-claimed experiments are actually present
claimed = ['exp_954', 'exp_964', 'exp_974']  # from synthesis summary
missing = [e for e in claimed if e not in ss_exp_ids]
if missing:
    print(f"SYNTHESIS WRITE FAILURE: {missing} claimed but not in self_state.json")
    # Create new synthesis task for missing experiments
```

**Decision flow after synthesis completes:**
1. Re-run Method A (done_exp_ids - ss_exp_ids)
2. If unsynthesized count decreased → synthesis worked, proceed
3. If unsynthesized count unchanged → SYNTHESIS WRITE FAILED, create new synthesis task
4. If counter drift detected → add counter reconciliation to next synthesis task body

**Include failure context in re-creation body:** When creating a new synthesis task because the prior one failed to write, include the failure context in the task body: "The previous synthesis task (v542) claimed to consolidate exp_1612/1613/1616 but did NOT actually write them to self_state.json." This prevents the new synthesis worker from trusting prior claims and ensures it actually performs the write. Observed in Cycle 265: synthesis task t_d58c6c28 claimed "Synthesis v542 complete. 3 experiments consolidated" but self_state.json still ended at exp_1611 (version bumped to 542, array unchanged).

**Alternative recovery — manual write (faster, for small batches):** When only 1-3 experiments are missing and their summaries are available from `kanban show`, the Director can manually add them to `self_state.json` instead of creating a new synthesis task. This is faster and avoids a second synthesis worker potentially failing again. Observed in Cycle #613: synthesis task t_9cb49e18 claimed consolidation of exp_2239/exp_2241 but wrote nothing. Manual add was faster than re-synthesizing.

```python
import json, os
from datetime import datetime, timezone

path = os.path.expanduser('~/.hermes/self_state.json')
ss = json.load(open(path))

# Add missing experiments (from kanban show summaries)
for exp_data in [
    {"id": "exp_2239", "title": "...", "result": "...", "status": "completed"},
    {"id": "exp_2241", "title": "...", "result": "...", "status": "completed"},
]:
    ss['experiments']['completed'].append(exp_data)

# CRITICAL: Update ALL metrics counters to match array length
ss['metrics']['experiments_completed'] = len(ss['experiments']['completed'])
ss['metrics']['experiments_conducted'] = len(ss['experiments']['completed'])
ss['metrics']['experiments_completed_count'] = len(ss['experiments']['completed'])
ss['metrics']['experiments_completed_list'] = [
    e.get('id', '') for e in ss['experiments']['completed'] if isinstance(e, dict)
]

ss['version'] = ss.get('version', 0) + 1
ss['last_updated'] = datetime.now(timezone.utc).isoformat()

with open(path, 'w') as f:
    json.dump(ss, f, indent=2, ensure_ascii=False)
```

**When to use manual vs re-synthesis:**
- Manual: 1-3 experiments, summaries available, want to fix immediately
- Re-synthesis: 4+ experiments, want full knowledge graph updates, synthesis worker is reliable

This is the same "ground truth vs claimed truth" principle that applies to queue item resolution — always verify against the durable store, not the transient claim.
