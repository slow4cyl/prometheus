# Topic Duplicates with Different IDs Slip Through Batch Creator

**Source:** Director Pass #246, Cycle 2026-06-03

## Problem

When the same research question appears in the curiosity queue under different synthesis versions (e.g., v535 and v626), each gets a different experiment ID and thread assignment. The batch creator's dedup checks don't catch these cross-thread duplicates.

## What Happened

4 pairs of identical topics were created as separate running tasks in one Director pass:

| Pair | Topic | Source Synthesis Versions |
|------|-------|---------------------------|
| exp_2483 ↔ exp_2511 | Standalone TF-IDF+LR wins at 100+ domains | v535, v535 |
| exp_2485 ↔ exp_2512 | C=50.0 optimal across ALL real injection types | v607, v607 |
| exp_2488 ↔ exp_2514 | Isotonic calibration production-scale needs | v629, v629 |
| exp_2492 ↔ exp_2518 | Oracle gap 0.0005 for ensemble sizing | v633, v633 |

All 8 tasks were dispatched and running simultaneously.

## Why It Happened

Three dedup layers exist but none caught this:

1. **Intra-queue dedup** (`curiosity_scorer.py`): Deduplicates queue items at scoring time via word overlap (>0.7 → score=0). But the duplicate items had slightly different text (different synthesis versions append different context), so overlap was below threshold.

2. **Intra-batch dedup** (`batch_create_tasks.py`): Checks each candidate against already-selected items before adding to batch. But both duplicates weren't in the same batch — they were selected in the same pass but the check compares against *earlier items in the same batch*, not all running tasks.

3. **Jaccard overlap against completed experiments**: The batch creator marks items as "covered" if their hypothesis/result overlaps with completed experiments. But running task titles use different phrasings, and the check is against *completed* experiments, not *running* tasks.

4. **Diversity cap** (35% per thread): Prevents same-thread monopolization but NOT cross-thread duplicates on the same topic. Both duplicates get different thread assignments.

## Root Cause

The batch creator's overlap check compares queue items against **completed experiments** (from self_state.json), not against **running task titles** (from kanban list). Different synthesis versions produce slightly different phrasings of the same question, and the Jaccard thresholds are tuned for hypothesis/result text, not queue item titles.

## Detection

After `batch_create_tasks.py` creates tasks, run `--dry-run` and visually compare new task titles against `kanban list --status running` titles. Look for:
- Same core question with different experiment IDs
- Same keywords but different synthesis version prefixes
- Same thread assignments (both in same thread = definitely duplicate)

## Fix (for batch_create_tasks.py)

Add a check that compares each candidate's normalized title against ALL running task titles (not just completed experiments):

```python
running_titles = [normalize(t['title']) for t in running_tasks]
for candidate in scored_items:
    candidate_norm = normalize(candidate['text'])
    overlap = max(jaccard(candidate_norm, rt) for rt in running_titles)
    if overlap > 0.5:  # same topic threshold
        candidate['score'] = 0  # mark as covered
```

## Impact

- Wastes 4 worker slots on redundant experiments
- Produces duplicate results that synthesis must merge
- No data corruption — synthesis handles duplicates gracefully
- But 4 workers × ~30min each = ~2 hours of wasted compute

## Prevention (manual, until batch creator is fixed)

Director should run `batch_create_tasks.py --dry-run` and cross-check output against `kanban list --status running --json` titles before committing task creation.
