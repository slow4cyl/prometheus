# Suffixed Experiment ID Mismatch — False Unsynthesis Detection

**Added:** Cycle #217  
**Severity:** Medium (wastes Director cycle time)  
**Category:** Director / unsynthesis detection

## Problem

Self_state.json stores experiment entries with suffixes (e.g., `exp_745_isotonic`, `exp_745_safety`) while done task titles contain unsuffixed IDs (e.g., `exp_745: Isotonic regression minimum ECE`).

The naive set difference `done_exp_ids - ss_exp_ids` treats these as different strings:
- Done task title regex extracts: `exp_745`
- Self_state entry: `exp_745_isotonic` (dict with id field) or string `exp_745_isotonic`
- Result: `exp_745` NOT IN `{'exp_745_isotonic', 'exp_745_safety'}` → flagged as unsynthesized

## Impact

- Director creates unnecessary synthesis tasks for already-consolidated experiments
- Wastes worker slots and cycle time
- In Cycle #217, exp_745 was flagged despite being consolidated in synthesis v344, v345, v348, v350, v351, v352, v354, v356

## Detection

When unsynthesis detection finds exactly 1-2 "unsynthesized" experiments, manually verify:
1. Check if any self_state entry STARTS WITH the unsynthesized ID (prefix match)
2. Search self_state.json for the base ID: `grep "exp_745" ~/.hermes/self_state.json`

## Fix

Normalize IDs before comparison. Two approaches:

### Approach 1: Strip suffixes from self_state entries
```python
import re
def normalize_eid(eid):
    """Strip suffixes like _isotonic, _safety from experiment IDs."""
    return re.sub(r'_\w+$', '', eid)

ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else str(e)
    if eid:
        ss_exp_ids.add(normalize_eid(eid))
```

### Approach 2: Prefix matching (more robust)
```python
def is_synthesized(unsynth_id, ss_exp_ids):
    """Check if unsynth_id is covered by any self_state entry (prefix match)."""
    return any(ss_id.startswith(unsynth_id) for ss_id in ss_exp_ids)
```

## Root Cause

Experiment entries in self_state.json are created by synthesis workers who sometimes append descriptive suffixes to distinguish experiment variants (e.g., exp_745 has two variants: isotonic regression and safety training degradation). The kanban task title uses the base ID, creating the mismatch.

## Related Pitfalls

- `mixed-experiment-id-formats-break-naive-sorting` (Cycle #166) — letter suffixes like `exp_104b` break int() conversion
- `same-topic-different-id-duplicates` (Cycle #185) — different IDs for same research question
- `synthesis-coverage-verification` — synthesis titles claim coverage not in self_state.json
