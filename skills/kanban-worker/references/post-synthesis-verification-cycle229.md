# Post-Synthesis Verification Step (Cycle #229)

## What Happened

Synthesis task t_52c2a818 completed with summary claiming "3 new experiments consolidated into self_state.json (915 total)." Director accepted this at face value. Verification 5 minutes later revealed:
- 0 of the claimed experiments (exp_954/964/974) were actually in self_state.json
- Array length: 905, metrics counter: 915 → drift of 10
- Synthesis worker reported success but write silently failed

## Required Director Workflow Change

Add step 8.5 to the Director Quick-Pass Flowchart (between DISPATCH and AUDIT LOG):

```
8. POST-SYNTHESIS VERIFICATION (if synthesis task completed this pass)
   Re-run unsynthesized detection (Method A: done_exp_ids minus ss_exp_ids)
   If count unchanged since before synthesis → SYNTHESIS WRITE FAILED
   If counter drift detected → add reconciliation to next synthesis task body
   If synthesis claimed experiments not present → create new synthesis task
```

## Detection Code

```python
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

# Compare against pre-synthesis unsynthesized count
# If unchanged, synthesis write failed
```

## Why This Matters

Without this verification, the Director proceeds to create new experiment tasks assuming synthesis covered the gap. The unsynthesized experiments remain unsynthesized indefinitely, and counter drift accumulates across cycles (observed: 3 → 10 → 163 over ~70 cycles).

## SKILL.md Update Needed

The kanban-worker SKILL.md (100,281 chars) exceeds the 100K patch limit. The flowchart update should be applied when the SKILL.md is next restructured or split into a SQLite companion. The relevant section is "Director Quick-Pass Flowchart (added Cycle #186)" — add step 8.5 after "7. DISPATCH" and renumber "8. AUDIT LOG" to "9. AUDIT LOG."
