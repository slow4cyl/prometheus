# Manual Fallback for Batch Creator Coverage False Positives

**Problem:** `batch_create_tasks.py` uses keyword-based Jaccard overlap to determine if queue items are "covered" by running tasks. This is too broad — it matches on individual words like "injection", "detection", "ensemble" which appear in many unrelated queue items. Result: the script reports "Uncovered by running tasks: N" but creates tasks that overlap with running experiments.

**Detection:** After `batch_create_tasks.py --dry-run`, manually check each selected item against running task titles. The script's coverage check uses word-level overlap; the manual check should use phrase-level overlap.

## 3-Word Phrase Overlap Pattern

This pattern catches topical overlaps that word-level matching misses, while avoiding false positives from shared stop words:

```python
import json, re, os

running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '').lower() for t in running]

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    words = text.lower().split()
    # Check 3-word phrases against running titles
    overlap_score = 0
    for j in range(len(words) - 2):
        phrase = ' '.join(words[j:j+3])
        if any(phrase in t for t in running_titles):
            overlap_score += 1
    # overlap_score >= 3 = likely covered, < 3 = genuinely different
    print(f'[{i}] overlap={overlap_score} | {text[:80]}')
```

**Thresholds (from Cycle #641):**
- overlap >= 5: HIGH overlap — item is covered by a running task. Do NOT create.
- overlap 3-4: MEDIUM — may be partially covered. Check manually.
- overlap <= 2: LOW — genuinely different topic. Safe to create.

## Why Word-Level Fails

The batch creator's word-level check sees "injection" in both "injection detection distributed across ALL Qwen layers" and "C=50.0 optimal across ALL real injection types" and marks both as overlapping. But they're investigating different questions (layer specialization vs. regularization parameter).

3-word phrases like "injection detection distributed" vs "injection types does this" are distinct enough to distinguish the topics.

## Fallback Workflow

1. Run `batch_create_tasks.py --count N --dry-run`
2. For each selected item, compute 3-word phrase overlap against running titles
3. Filter out items with overlap >= 3
4. Create tasks only for the remaining non-overlapping items
5. If batch creator selects < 3 non-overlapping items, create manually from queue

## Integration with Director Pass

This check should happen after step 5 (CHECK SATURATION) and before step 6 (CREATE EXPERIMENT TASKS) in the Director flowchart. It's a filter on the batch creator's output, not a replacement for it.
