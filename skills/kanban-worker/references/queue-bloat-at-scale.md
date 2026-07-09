# Queue Bloat at Scale — Cycle #244

## Problem

The curiosity queue grew to 1,228 items while all 21 workers were busy. The Director's classification code processes only `queue[:50]` — missing 96% of the queue. Many items are duplicates (same question 3-5x with slightly different wording).

## Observed State (Cycle #244)

```
Queue size: 1,228 items
Running tasks: 21 (saturated)
Free workers: 0
Unsynthesized experiments: 0
Done tasks: 2,110
```

### Queue Classification (first 50 items only)
- RESOLVED: 32
- RUNNING: 0
- DONE: 14
- NO_SOURCE: 4

### NO_SOURCE Items (332 total, 41 with LOW coverage)
Many are duplicates of the same research questions:
- "Reference-deference on Qwen3.6" appears 3x (indices 405, 1008, 1013)
- "Batch inflation temperature sensitivity" appears 2x (indices 406, 1009)
- "GPT-4o reference-deference REFUTED" appears 2x (indices 410, 1013)

## Root Cause

1. **Curation threshold too low**: The director-loop skill sets curation at 50 items, but experiments generate 5-10 new curiosities per cycle. At 20+ cycles without curation, queue grows unboundedly.
2. **Duplicate generation**: Synthesis workers add curiosities that overlap with existing items. No deduplication at append time.
3. **Curation task competition**: Curation is maintenance, but workers prioritize experiment tasks. When saturated, curation never runs.

## Fix

### Immediate (Director pass)
When queue > 200, create curation task as priority maintenance — even during saturation. Curation is maintenance, not investigation, so the saturation threshold doesn't apply.

### Structural (synthesis worker)
Cap queue at 200 items. When appending new curiosities:
1. Score all items by priority
2. Deduplicate by topic similarity (not exact match)
3. Remove lowest-scored items if over cap
4. Always preserve high-priority items regardless of cap

### Classification improvement
Process ALL queue items, not just `queue[:50]`. Use batched classification for large queues:

```python
# Instead of:
for item in queue[:50]:

# Use:
for i, item in enumerate(queue):
    # Classification logic
```

Or process in batches of 100 with intermediate saves.

## Deduplication Pattern

```python
from collections import defaultdict
import re

def deduplicate_queue(queue):
    """Group queue items by normalized topic, keep highest-scored per group."""
    groups = defaultdict(list)
    for i, item in enumerate(queue):
        text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
        if 'RESOLVED' in text:
            continue
        # Normalize: lowercase, remove experiment IDs, remove prefixes
        normalized = re.sub(r'exp_\d+\w*', '', text.lower())
        normalized = re.sub(r'\[.*?\]', '', normalized)  # Remove bracketed prefixes
        normalized = re.sub(r'[^a-z0-9\s]', '', normalized)
        normalized = ' '.join(normalized.split())  # Collapse whitespace
        groups[normalized].append((i, item))
    
    deduped = []
    for group_items in groups.values():
        # Keep the item with highest priority score
        best = max(group_items, key=lambda x: x[1].get('priority', 0) if isinstance(x[1], dict) else 0)
        deduped.append(best[1])
    
    return deduped
```
