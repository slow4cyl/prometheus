# Silent Synthesis Failure — Detection and Reconciliation

## Problem

A synthesis task can complete successfully (via `kanban_complete` with a valid summary) but **silently fail** to write updates to `self_state.json`. The synthesis worker's summary reports success ("Consolidated N experiments, Total experiments: X"), but the `experiments.completed` array in self_state.json doesn't include the newly synthesized experiments. This creates a persistent counter drift.

**Observed:** Cycle #205 — synthesis task t_cd31254d completed with summary "Consolidated 4 experiments: exp_632, exp_642, exp_643, exp_646. Total experiments: 582." But self_state.json showed metrics.experiments_completed=582 while experiments.completed array had only 578 entries (the 4 missing experiments were never written).

## Detection Pattern (Method A)

The most reliable detection is comparing done task IDs against self_state.json's `experiments.completed` array:

```python
import json, re, os

# Load done tasks
done = json.load(open('/tmp/kanban_done.json'))
done_exp_ids = set()
for t in done:
    title = t.get('title', '')
    is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
    if not is_synth:
        for m in re.finditer(r'exp_(\d+\w*)', title):
            done_exp_ids.add(f'exp_{m.group(1)}')

# Load self_state
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    if isinstance(e, dict):
        ss_exp_ids.add(e.get('id', ''))
    elif isinstance(e, str):
        m = re.search(r'exp_(\d+\w*)', e)
        if m:
            ss_exp_ids.add(f'exp_{m.group(1)}')

unsynth = done_exp_ids - ss_exp_ids
if unsynth:
    print(f"UNSYNTHESIZED: {len(unsynth)} experiments not in self_state.json:")
    for e in sorted(unsynth, key=lambda x: int(re.search(r'(\d+)', x).group(1)) if re.search(r'(\d+)', x) else 0):
        print(f"  {e}")
```

If this returns experiments that have completed tasks but aren't in self_state.json, a synthesis write failed silently.

## Reconciliation

Create a dedicated synthesis task with explicit counter reconciliation instructions:

```
SYNTHESIS CYCLE: Read self_state.json FIRST (source of truth).

Experiments to synthesize:
- exp_XXX: <one-line summary from done task>
- exp_YYY: <one-line summary from done task>

CRITICAL: Counter reconciliation required. Previous synthesis completed but did NOT
update self_state.json. Metrics show N completed but experiments.completed array has
only M entries (K missing: <list IDs>). Fix this drift.

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \
  --task-id $HERMES_KANBAN_TASK \
  --experiments "exp_NNN,..." \
  --resolutions '[{"item": N, "resolved_by": "exp_XXX", "note": "..."}]' \
  --curiosities "Follow-up 1?;Follow-up 2?"
This writes to the synthesis_outputs table. synthesis_merger.py (cron 2m) applies to self_state.json.
```

## Why This Happens

The synthesis worker may fail to write to self_state.json due to:
1. TIRITH filter blocking the write (escape patterns, API keys in content)
2. JSON serialization error (non-serializable data in experiment entries)
3. File permission issue
4. The synthesis worker's `kanban_complete` call succeeds before the self_state.json write completes (race condition)

The worker reports success because `kanban_complete` is called after the intended write, but if the write silently failed, the completion summary is misleading.

## Prevention

The Director should ALWAYS run Method A detection at the start of each pass, regardless of whether synthesis tasks recently completed. This catches silent failures that the synthesis worker's summary doesn't report.
