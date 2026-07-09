# Director Queue Deduplication

## Problem (observed Cycle #214)

Synthesis workers can generate identical queue items across multiple cycles. In Cycle #214, 7 out of 43 queue items were identical copies of "Is the confident-but-wrong vulnerability (exp_722) exploitable for targeted fact injection?" — all tagged `[NEW from synthesis v347]`. This inflates the queue, wastes curation time, and can cause duplicate task creation.

## Detection Algorithm

After classification, group items by normalized text to find duplicates:

```python
from collections import defaultdict
import re

def detect_queue_duplicates(queue):
    """Find groups of near-identical queue items."""
    groups = defaultdict(list)
    for i, item in enumerate(queue):
        text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
        if 'RESOLVED' in text:
            continue
        # Normalize: lowercase, strip experiment refs, strip synthesis tags
        normalized = re.sub(r'\[NEW from [^\]]+\]', '', text)
        normalized = re.sub(r'exp_\d+\w*', '', normalized)
        normalized = re.sub(r'\[.*?\]', '', normalized)  # strip bracket tags
        normalized = re.sub(r'\s+', ' ', normalized).lower().strip()
        groups[normalized].append((i, text))
    
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    return duplicates
```

## Fix During Curation

When duplicates are found:
1. Keep the earliest item (lowest index) — it has the most context
2. Tag later duplicates as `[RESOLVED — duplicate of item N]` (or remove from queue entirely if the queue is stored as a list)
3. Include deduplication count in the Director pass audit log

## Prevention

Synthesis workers should check the existing queue before appending new items. If a question with similar semantic content already exists, skip or merge rather than append. This requires the synthesis worker to:
1. Read the current queue from self_state.json
2. Compute normalized text for each existing item
3. Before appending a new item, check if a similar normalized text already exists
4. If yes, skip (the existing item already captures the question)

## When to Run Deduplication

- **Always during queue curation** (when queue > 50 items triggers a curation task)
- **During Director passes** when classifying queue items — run dedup detection first to avoid creating tasks for duplicate items
- **During synthesis** — synthesis workers should check for existing similar items before appending

## Example (Cycle #214)

Queue had 43 items. After dedup detection:
- 7 items were identical ("confident-but-wrong vulnerability exp_722")
- 1 item was the original, 6 were duplicates
- Effective unique items: 37 (not 43)
- Without dedup, the Director might create tasks for duplicate items, wasting worker slots
