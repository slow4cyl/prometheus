# Batch Creator Coverage Still Too Broad (Cycle #245)

## Problem

`batch_create_tasks.py --count N --min-score 30` reported "Uncovered by running tasks: 19" and selected 2 tasks:
1. Cross-dataset few-shot adaptation (score 88, thread: transfer)
2. Isotonic calibration production-scale (score 88, thread: calibration)

Both had HIGH keyword overlap (0.56) with running tasks:
- Queue [29] (cross-dataset few-shot) overlaps running t_bc871569 (exp_2339: Cross-dataset few-shot adaptation)
- Queue [30] (isotonic calibration) overlaps running t_7e187934 (exp_2367: Isotonic calibration production-scale)

The batch creator's internal coverage check failed to detect these overlaps.

## Root Cause

The batch creator uses Jaccard overlap on tokenized text, but the threshold for "covered" is still too permissive for items that share the same research question under slightly different wording. The running task title "exp_2339: [NEW from synthesis v626] Cross-dataset few-shot adaptation — can 5-10 target-do" and queue item "[NEW from synthesis v626] Cross-dataset few-shot adaptation — can 5-10 target-domain samples close the 14pp meta-classif" share ~56% keyword overlap but the batch creator's internal check didn't flag them.

## Fix: Manual Post-Creation Verification

After `batch_create_tasks.py` selects tasks, ALWAYS verify with manual keyword overlap before actually creating:

```python
import json, re, os

running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '') for t in running]

stop = {'the', 'a', 'an', 'is', 'are', ...}  # standard stop words

for selected_item in batch_selected_items:
    text = selected_item['text']
    words = set(text.lower().split())
    keywords = {w for w in words - stop if not re.match(r'^exp_\d+\w*$', w)}
    running_titles_text = ' '.join(t.lower() for t in running_titles)
    overlap = sum(1 for w in keywords if w in running_titles_text)
    coverage = overlap / max(len(keywords), 1)
    if coverage > 0.3:
        print(f"SKIP: {text[:80]} — HIGH coverage ({coverage:.2f}) with running tasks")
    else:
        print(f"CREATE: {text[:80]} — LOW coverage ({coverage:.2f})")
```

## Threshold History

| Cycle | Threshold | Result |
|-------|-----------|--------|
| #245 | 0.4 (hypothesis), 0.45 (results), 0.5 (running) | 41/52 filtered as "already answered" — too aggressive |
| #583 | 0.6, 0.65, 0.6 | Active items increased 11→33 |
| #601 | 0.6, 0.75, 0.6 | Still filtering legitimate follow-ups |
| #245 (manual) | 0.15 (keyword overlap) | Caught overlaps batch creator missed |

The batch creator's Jaccard-based check and the manual keyword-overlap check use different algorithms. The manual check is more reliable for detecting same-topic-different-wording overlaps.

## Recommendation

Add a `--verify` flag to `batch_create_tasks.py` that runs the manual keyword overlap check on selected items and warns/skips HIGH-coverage items. Or: always run the manual check as a post-step in the Director workflow before calling `kanban_create`.
