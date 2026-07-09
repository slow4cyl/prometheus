# Batch Creator Cross-Cycle Duplication (Cycle #245, updated Cycle #603)

## Problem

The `batch_create_tasks.py` script checks queue items against *completed* experiments for coverage and *does* have a running task overlap check (Jaccard >0.6 on full title text). However, this running task check is **unreliable** — it uses raw title text, not normalized topics. Tasks with the same topic but different title prefixes (e.g., "exp_2692: feature-augmented hybrid" vs "exp_2727: exp_2692: feature-augmented hybrid") don't trigger the Jaccard threshold. The batch creator reports "Uncovered by running tasks: N" but the count is inaccurate.

When synthesis re-adds queue items whose source experiment is still running, or when queue items share topic overlap with running tasks, the batch creator treats them as uncovered and creates new tasks — even though identical topics are already being investigated.

## Observed Impact

**Cycle #245:** 23 running tasks, 19 were topic duplicates across 6 threads. Isotonic calibration: 6 duplicate tasks. TF-IDF+LR: 5 duplicate tasks. Only 4 genuinely unique experiments running. 14 workers wasted.

**Cycle #603:** After manually blocking 13 topic duplicates (7 threads with 2-3x duplication), the batch creator immediately created 4 NEW tasks that overlapped with existing running topics. The batch creator's "Uncovered by running tasks: 17" count was wrong — 4 of the 4 selected tasks were duplicates. Had to block all 4 immediately after creation.

## Detection Pattern

After dumping running tasks to `/tmp/kanban_running.json`:

```python
from collections import defaultdict
import re

topics = defaultdict(list)
for t in running:
    title = t.get('title', '')
    m = re.search(r'exp_(\d+):\s*(.+)', title)
    if m:
        topic = m.group(2)[:60].lower().strip()
        topics[topic].append(t['id'])

dupes = {k: v for k, v in topics.items() if len(v) > 1}
for topic, ids in dupes.items():
    print(f"TOPIC DUPLICATE: {topic} -> {ids}")
```

## Reclamation Pattern

For each duplicate group, keep the oldest task (lowest exp number), reclaim the rest:

```bash
# Reclaim duplicates (keep the one with lowest exp number)
hermes kanban reclaim <duplicate_task_id>

# Kill zombie processes from reclaimed tasks
ps aux | grep "kanban task <reclaimed_id>" | grep -v grep | awk '{print $2}' | xargs kill
```

## Queue Classification Context

When checking queue coverage, items fall into these categories:
- **RUNNING**: Source experiment is running — item will resolve naturally. Do NOT create tasks.
- **OPEN**: No running task covers this topic. Candidate for task creation.
- **RESOLVED**: Already tagged. Skip.

The batch creator only checks against completed experiments, missing the RUNNING category entirely.

## Root Cause

The batch creator's overlap checks operate on:
1. Completed experiments (Jaccard >0.6 on hypothesis text)
2. Already-selected batch items (Jaccard >0.7)
3. Running task titles (Jaccard >0.6 on full title text) — **this exists but is unreliable**

The running task check fails because:
- Jaccard on raw titles doesn't catch topic matches with different prefixes (e.g., "exp_2692: feature-augmented hybrid" vs "exp_2727: exp_2692: feature-augmented hybrid")
- The "Uncovered by running tasks: N" count is advisory, not authoritative
- Title text includes experiment IDs, synthesis tags, and status prefixes that dilute the overlap score

## Fix: Normalized Topic Comparison (preferred over Jaccard)

Instead of Jaccard on raw titles, normalize to topics and check overlap:

```python
import re
from collections import defaultdict

def get_topic(title):
    """Extract normalized topic from task title."""
    cleaned = re.sub(r'^exp_\d+:\s*', '', title)
    cleaned = re.sub(r'\[.*?\]\s*', '', cleaned)
    return cleaned[:60].strip().lower()

# Build running topic set
running_topics = set()
for t in running_tasks:
    running_topics.add(get_topic(t['title']))

# Before adding item to batch, check topic overlap
item_topic = get_topic(item_title)
for rt in running_topics:
    # Word-level overlap on normalized topics
    words_item = set(item_topic.split())
    words_running = set(rt.split())
    overlap = len(words_item & words_running) / max(len(words_item), 1)
    if overlap > 0.4:
        print(f"SKIP: topic '{item_topic}' overlaps running '{rt}' ({overlap:.0%})")
        break
```

## Detection: Verify Batch Creator Output

After `batch_create_tasks.py` creates tasks, ALWAYS verify each task's topic against running task titles BEFORE dispatching:

```python
# After batch creation, before dispatch
for created_task in batch_created_tasks:
    created_topic = get_topic(created_task['title'])
    for running_task in running_tasks:
        running_topic = get_topic(running_task['title'])
        if created_topic == running_topic:
            print(f"DUPLICATE: {created_task['id']} overlaps {running_task['id']}")
            # Block the created task
            subprocess.run(['hermes', 'kanban', 'block', created_task['id'],
                          'topic-duplicate: same experiment running on another worker'])
```

This catches duplicates the batch creator's Jaccard check misses.

## Prevention

1. **Always run topic dedup detection BEFORE creating tasks** (step 5b in Director flowchart). This catches duplicates that the batch creator's automated checks miss.

2. **Verify batch creator output before dispatch.** After `batch_create_tasks.py` creates tasks, parse each created task's normalized topic against running task topics. Block any duplicates before calling `hermes kanban dispatch`.

3. **Don't trust "Uncovered by running tasks: N".** The count is advisory. Always do your own topic comparison.

4. **Block, don't reclaim.** Blocking removes tasks from dispatch entirely. Reclaiming causes immediate re-dispatch (the task goes running → ready → running within seconds).
