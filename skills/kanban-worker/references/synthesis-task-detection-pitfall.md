# Synthesis Task Detection Pitfall (Cycle #591)

## Problem

When checking if a synthesis task is already running, using broad string matching
like `'synth' in title.lower()` or `'consolidat' in title.lower()` produces false
positives because experiment titles frequently reference synthesis in their body text.

## Example

Running tasks in Cycle #591:
```
exp_2098: Can diversity_entropy predict difficulty in real-world
exp_2101: [NEW from synthesis v578] Isotonic calibration scaling law N^{-0.316}
exp_2102: [NEW from synthesis v587] exp_2081 REVERSED: dispatch_overhead is a DOMAIN ANCHOR
exp_2110: [NEW from synthesis v589] Negative signal generalization
...
```

Using `'synth' in title.lower()` matches 9 tasks (all the "from synthesis vXXX" references).
Actual synthesis task count: 0.

## Fix

Synthesis task titles begin with "SYNTHESIS:" or "consolidat". Experiment titles begin
with "exp_NNN:". This is the reliable discriminator:

```python
# WRONG — matches experiment titles referencing synthesis
synth_tasks = [t for t in running if 'synth' in t.get('title', '').lower()]

# CORRECT — only matches actual synthesis tasks
synth_tasks = [t for t in running if t.get('title', '').startswith('SYNTHESIS') or t.get('title', '').lower().startswith('consolidat')]
```

## Why This Matters

- Inflated synthesis task count causes the Director to skip creating a needed synthesis task
- Or creates duplicate synthesis tasks thinking none exist
- Both waste worker slots and delay consolidation

## Related

- See `unsynthesis-detection-variant3-impact.md` for how list omission affects unsynthesis detection
- See Director Quick-Pass Flowchart step 3 (DETECT UNSYNTHESIZED) in main SKILL.md
