# Batch Creator Topic Overlap Pitfall (Cycle #244)

## Problem

The `batch_create_tasks.py --dry-run` reports "Uncovered by running tasks: N" based on **experiment ID matching** — it checks if queue item source experiment IDs appear in running task titles. This misses **TOPIC overlaps** where running experiments investigate the same question under different experiment IDs.

## Example (Cycle #244)

Batch creator reported: `Uncovered by running tasks: 18, Selected: 6 tasks`

But 2 of the 6 selected tasks overlapped with running experiments:
- "Cross-dataset few-shot adaptation" — 4 running experiments already on this topic
- "Isotonic calibration production-scale" — 3 running experiments already on this topic

The batch creator's ID-based check didn't catch these because the queue items referenced different experiment IDs than the running tasks.

## Detection

After `--dry-run`, manually cross-reference selected items' topic text against `kanban list --status running --json` titles:

```python
import json, re

running = json.load(open('/tmp/kanban_running.json'))
running_titles = ' '.join(t.get('title', '').lower() for t in running)

# For each selected item from dry-run:
for item in selected_items:
    # Extract core topic (text after exp_NNN: or [NEW from synthesis...])
    topic_words = set(re.sub(r'[^a-z0-9 ]', '', item.lower()).split())
    # Remove stop words and experiment IDs
    stop = {'the', 'a', 'an', 'is', 'are', 'can', 'we', 'this', 'that', 'new', 'from', 'synthesis'}
    topic_words -= stop
    topic_words = {w for w in topic_words if not re.match(r'^exp_\d+$', w)}
    
    overlap = sum(1 for w in topic_words if w in running_titles)
    coverage = overlap / max(len(topic_words), 1)
    
    if coverage > 0.3:
        print(f"HIGH OVERLAP ({coverage:.2f}): {item[:60]}")
        # Replace with genuinely uncovered NO_SOURCE item
```

## Fix

When batch creator selects items with HIGH keyword overlap (>0.3) against running titles:
1. Filter out the overlapping items
2. Replace with genuinely uncovered items from the NO_SOURCE queue category
3. Verify replacement items don't themselves overlap with running experiments

The batch creator's `--min-score 30` flag helps with scoring but doesn't solve the topic-overlap problem.

## Why This Happens

The batch creator's coverage check (`is_covered_by_running()`) uses Jaccard similarity on experiment IDs extracted from queue item text. It matches `exp_2386` in the queue against `exp_2386` in running tasks. But if the queue item says "Cross-dataset few-shot adaptation" without an experiment ID (NO_SOURCE category), or references a different experiment ID that investigated the same topic, the ID-based check returns "uncovered" even though the topic is well-covered.
