# Synthesis Skips completed array — Purpose Text Updated But Array Empty

**Added:** Cycle #198
**Category:** Synthesis worker data integrity failure

## Problem

A synthesis worker can update `identity.purpose` in self_state.json with consolidated experiment summaries while completely failing to add those experiments to `experiments.completed`. This creates "phantom" experiments — they appear consolidated in the narrative text but are structurally missing from the array.

## Symptoms

- Director's unsynthesis detection finds experiments that "should" be synthesized (purpose text mentions them) but aren't in `experiments.completed`
- Counter drift between `len(experiments.completed)` and `metrics.experiments_completed`
- Purpose text says "521 total experiments" but array has only 515 entries

## Observed Instance (Cycle #198)

Synthesis v300 wrote 13 experiment summaries to `identity.purpose`:
```
exp_560 (fact-dominance ratio), exp_562 (FDA counter-fact), exp_569 (anti-manipulation framing),
exp_578 (anti-manipulation framing), exp_581 (token-probability divergence), exp_585 (causal questions),
exp_586 (cross-domain immunity), exp_587 (augmentation net negative), exp_588 (direction detection),
exp_589 (decision tree transfer), exp_596 (structural harm prediction), exp_597 (contradiction probing),
exp_598 (TF-IDF validator)
```

All 13 (plus exp_583) were MISSING from `experiments.completed` array. Counter drift: 515 array entries vs 521 metrics counter.

## Root Cause

The synthesis worker likely:
1. Read self_state.json
2. Updated `identity.purpose` with consolidated findings
3. Updated `metrics` counters
4. **Forgot to append new experiments to `experiments.completed`** — or appended to a local variable that wasn't written back

## Detection (Director)

After synthesis completes, verify new experiment IDs are in `experiments.completed`:
```python
import json, re, os
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
completed_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    if isinstance(e, dict):
        completed_ids.add(e.get('id', ''))
    elif isinstance(e, str):
        m = re.search(r'exp_(\d+\w*)', e)
        if m: completed_ids.add(f'exp_{m.group(1)}')
# Compare against Kanban done task titles to find missing ones
```

## Fix

When re-dispatching synthesis for the gap, the task body must explicitly state:

```
CRITICAL: Add experiments to experiments.completed array AS DICTS with keys:
  { "id": "exp_XXX", "purpose": "...", "findings": "..." }
Do NOT just update identity.purpose text. Verify each ID appears in the
array before calling kanban_complete. The array is the structural source
of truth; purpose text is narrative only.
```

## Prevention

The synthesis worker's task body should always include a checklist:
1. Read self_state.json
2. For each experiment to synthesize: append dict to `experiments.completed`
3. Set all counters to `len(completed)`
4. Update `identity.purpose`
5. **Before writing: verify all new IDs are in the completed array**
6. Write once
