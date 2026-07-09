# Synthesis Task Title Matching — False Positive Pitfall

**Added: Cycle #250**

## Problem

Searching for synthesis tasks by matching `'synth' in title.lower()` matches experiment task titles that contain `[NEW from synthesis vXXX]` in their bracket text — NOT actual synthesis tasks.

### Example (Cycle #250)

Running tasks included:
```
exp_2476: [NEW from synthesis v535] Standalone TF-IDF+LR wins at 100+ domains
exp_2477: [NEW from synthesis v607] C=50.0 optimal across ALL real injection types
exp_2493: [NEW from synthesis v636] RF exceeds Bayes ceiling (AUC=0.9801 vs 0.898)
```

All 13 experiment tasks matched `'synth' in title.lower()` because "synthesis" appears in the `[NEW from synthesis vXXX]` bracket text. The actual synthesis task title format is:
```
SYNTHESIS: consolidate exp_182, exp_811, exp_2448, exp_2527
```

## Correct Pattern

```python
# GOOD — matches only actual synthesis task titles
is_synth = title.upper().startswith('SYNTHESIS:') or 'SYNTHESIS:' in title.upper()

# BAD — matches experiment tasks with synthesis source brackets
is_synth = 'synth' in title.lower()  # false positive on "[NEW from synthesis vXXX]"
```

## Impact

- Incorrectly identifies experiment tasks as synthesis tasks
- Skips creating synthesis tasks when none exist (false "synthesis already running")
- Can cause unsynthesized experiments to go unprocessed

## Related

- See `director-loop` skill pitfalls for unsynthesis detection patterns
- See `director-pass-checklist-cycle272.md` for the full Director pass workflow
