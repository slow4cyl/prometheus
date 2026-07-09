# Batch Creator Coverage Check False-Negative (Cycle #245)

## Problem

`batch_create_tasks.py --dry-run` reports "Uncovered by running tasks: 0" while 8+ workers are free and queue items are genuinely uncovered.

## Root Cause

The coverage check uses keyword matching against running task titles. Common words like "injection", "detection", "AUC", "F1", "ensemble" appear in many queue items AND running tasks, causing false coverage signals.

Example: Queue item "Meta-classifier AUC 0.99-1.00 on MULTI_LANG/OBFUSCATION — does this transfer?" gets marked COVERED because "AUC" appears in a running task about "Ensemble disagreement AUC 0.84-0.97" — completely different topics.

## Detection

```
python3 ~/.hermes/scripts/batch_create_tasks.py --count <free_workers> --dry-run
```

If output shows:
- `Uncovered by running tasks: 0`
- `Selected: 0 tasks`
- But `Workers: N free` where N ≥ 3

Then the coverage check is producing false negatives.

## Fix Options

### Option 1: Lower min-score threshold (quick fix)
```bash
python3 ~/.hermes/scripts/batch_create_tasks.py --count <free_workers> --min-score 40 --dry-run
```
Default min-score is 60. Lowering to 40 bypasses aggressive coverage filtering.

### Option 2: Manual task creation (reliable)
When batch creator fails, manually create tasks for the highest-priority uncovered items:

1. Load queue from self_state.json
2. Extract running task titles from kanban list
3. Identify queue items whose keywords DON'T appear in any running title
4. Create tasks for the top N items (where N = free workers)

### Option 3: Fix the coverage check (architectural)
The coverage check needs semantic matching, not keyword matching. Options:
- Use TF-IDF similarity instead of keyword overlap
- Use the RAG embedding server (port 9150) for semantic coverage
- Require exact phrase match, not individual word match

## Worked Example (Cycle #245)

Board state: 7 running tasks, 15 free workers
Queue: 22 active items, top priority items at score 75 and 66

Batch creator with default settings:
```
Queue: 22 active items, 2 score >= 60
Workers: 15 free, 7 busy
Uncovered by running tasks: 0
Selected: 0 tasks (diversity cap: 5/thread)
```

Batch creator with `--min-score 40`:
```
Queue: 22 active items, 21 score >= 40
Workers: 15 free, 7 busy
Uncovered by running tasks: 13
Selected: 11 tasks (diversity cap: 5/thread)
```

Result: 10 tasks created manually (batch creator's 11 included 4 duplicates of running tasks).

## Thread-Masking Variant (Cycle #245, min-score 10)

Even at `--min-score 10`, the batch creator may only select items from a single thread (e.g., "other") while 20+ free workers remain. This happens when running tasks use keywords that overlap with ALL major queue threads (attack, injection, tfidf), causing the coverage check to mark them all as "covered."

**Example:** 23 free workers, 50 queue items, min-score 10:
```
Uncovered by running tasks: 11
Selected: 8 tasks (diversity cap: 8/thread)
Thread distribution: other: 8
```

All 8 tasks came from the "other" thread because running tasks contained keywords like "injection", "detection", "adversarial" that matched queue items in attack/injection/tfidf threads.

**Root cause:** The batch creator's coverage check uses word-level keyword overlap, not semantic similarity. A running task about "homoglyph attacks" marks queue items about "adversarial training" as covered because both contain "adversarial".

**Fix:** After batch creation, manually create tasks for remaining free workers using queue items from threads that were falsely marked as covered. Verify by checking that the queue item's SPECIFIC topic differs from running tasks, not just shared keywords.

## Prevention

1. Run `--dry-run` with `--min-score 10` to see full coverage picture
2. If batch creates < 50% of free worker slots, switch to manual creation
3. Manually pick items from threads that appear "covered" but have genuinely different topics
4. Always verify: does this queue item ask a question a running task is ALREADY investigating? If not, create it.
