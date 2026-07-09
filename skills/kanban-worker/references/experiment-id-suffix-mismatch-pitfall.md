# Experiment ID Suffix Mismatch — False Unsynthesis Positives

**Added:** 2026-06-01 (Cycle #216)

## Problem

When the Director runs unsynthesis detection (Method A: `done_exp_ids - ss_exp_ids`), experiment IDs are extracted from done task titles via regex (`exp_(\d+)`). But `self_state.json` may store the same experiment with a suffix (e.g., `exp_745_isotonic`, `exp_745_safety`).

The exact-match regex produces `exp_745` from the title, but self_state has `exp_745_isotonic` — no match. Result: the experiment appears "unsynthesized" when it has actually been synthesized multiple times.

## Example (Cycle #216)

```
Done task title: "exp_745: Isotonic regression minimum ECE"
Extracted ID: exp_745

self_state.json experiments.completed:
  - exp_745_isotonic
  - exp_745_safety

Exact match: exp_745 NOT in {exp_745_isotonic, exp_745_safety} → FALSE POSITIVE
```

The experiment had been synthesized 5+ times (5 synthesis tasks in the done list), but the exact match missed all of them.

## Fix

After exact-match extraction, run a prefix match:

```python
# After computing unsynth = done_exp_ids - ss_exp_ids
false_positives = set()
for uid in unsynth:
    for ssid in ss_exp_ids:
        if ssid.startswith(uid) or uid.startswith(ssid):
            false_positives.add(uid)
            break
unsynth = unsynth - false_positives
```

## Detection

If the unsynthesis count is 1-3 items and the item has been synthesized multiple times (check done list for synthesis tasks mentioning it), suspect a suffix mismatch before creating a synthesis task.

## Related

- See "Identify unsynthesized experiments" section in kanban-worker SKILL.md (Method A)
- See "synthesis title/summary claims don't match actual coverage" pitfall
