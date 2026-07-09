# Topic-Based Duplicate Detection (Cycle #245)

## Problem

Two or more tasks investigate the same research question under different experiment IDs (e.g., exp_2202, exp_2223, exp_2254, exp_2261 all testing "EU AI Act deference 20% drop"). The ID-based intersection check misses these because IDs differ.

## Detection Pattern

Extract the topic text after the experiment ID and group by normalized topic:

```python
import json, re
from collections import defaultdict

running = json.load(open('/tmp/kanban_running.json'))
topics = defaultdict(list)

for t in running:
    title = t.get('title', '')
    m = re.search(r'exp_(\d+):\s*(.+)', title)
    if m:
        exp_id = f'exp_{m.group(1)}'
        # Normalize: lowercase, strip synthesis prefix, take first 60 chars
        topic = m.group(2).lower().strip()
        topic = re.sub(r'\[new from synthesis v\d+\]\s*', '', topic)
        topic = topic[:60]
        topics[topic].append({'id': t['id'], 'exp_id': exp_id, 'worker': t.get('assignee', '')})

# Find duplicates
for topic, items in sorted(topics.items(), key=lambda x: -len(x[1])):
    if len(items) > 1:
        print(f"DUPLICATE: {topic}")
        for item in items:
            print(f"  {item['exp_id']} ({item['id']}) -> {item['worker']}")
```

## Resolution

For each duplicate group:
1. Keep the oldest task (lowest exp number)
2. Reclaim newer duplicates: `hermes kanban reclaim <task_id>`
3. Block reclaimed tasks to prevent re-dispatch: `hermes kanban block <task_id> "duplicate of <oldest_task_id>"`

## Worked Example (Cycle #245)

Found 6 duplicate groups:
- EU AI Act deference: 4 tasks (exp_2202, exp_2223, exp_2254, exp_2261)
- C=50.0 optimal: 2 tasks (exp_2230, exp_2257)
- Ensemble disagreement: 2 tasks (exp_2231, exp_2264)
- dispatch_overhead: 2 tasks (exp_2243, exp_2253)
- Direct LR 36x faster: 2 tasks (exp_2256, exp_2263)
- TF-IDF+LR recall: 2 tasks (exp_2260, exp_2266)

Kept 6 oldest, reclaimed 6 newer duplicates. Freed 6 worker slots for new experiments.

## When to Run

At the start of every Director pass, after dumping board state. This catches duplicates created by:
- Batch creator creating tasks for items already covered by running experiments
- Synthesis workers adding the same curiosity multiple times across cycles
- Director creating tasks without checking running task topics

## Pitfall — Reclaimed tasks may still be running

After `hermes kanban reclaim`, the worker process may still be alive (zombie). Check `ps aux | grep prometheus-worker-N` after reclaiming. If the process is alive, kill it:
```bash
ps aux | grep 'prometheus-worker-N' | grep -v grep | awk '{print $2}' | xargs -r kill
```
