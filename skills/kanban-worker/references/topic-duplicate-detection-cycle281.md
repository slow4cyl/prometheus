# Topic-Based Duplicate Detection — Cycle #281 Verified Pattern

## Problem
ID-based duplicate detection (`set intersection of exp_NNN from task titles`) misses same-topic duplicates where different experiment IDs investigate the identical research question. In Cycle #281, 12 running tasks were duplicates across 4 topics:
- 8x "Cross-dataset few-shot adaptation — can 5-10 target-domain samples..."
- 4x "Isotonic calibration production-scale needs retry..."
- 2x "C=50.0 optimal across ALL real injection types..."
- 2x "Standalone TF-IDF+LR wins at 100+ domains..."

## Detection Pattern

```python
import json, re
from collections import defaultdict

running = json.load(open('/tmp/kanban_running.json'))
topic_tasks = defaultdict(list)

for t in running:
    title = t.get('title', '')
    m = re.search(r'exp_(\d+\w*):\s*(.+)', title)
    if m:
        exp_num = int(re.search(r'(\d+)', m.group(1)).group(1))
        topic = m.group(2)[:60].strip()  # Normalize to first 60 chars
        topic_tasks[topic].append((exp_num, t['id'], title[:100]))

# Find duplicates: keep oldest (lowest exp_num), reclaim rest
for topic, tasks in sorted(topic_tasks.items()):
    if len(tasks) > 1:
        tasks_sorted = sorted(tasks, key=lambda x: x[0])
        keep = tasks_sorted[0]
        reclaim = tasks_sorted[1:]
        print(f'Duplicate: {topic[:50]} ({len(tasks)} tasks)')
        for num, tid, title in reclaim:
            print(f'  RECLAIM: exp_{num} ({tid})')
```

## Reclaim + Block Workflow

After identifying duplicates, use reclaim-then-block to prevent re-dispatch:

```bash
# 1. Reclaim the duplicate tasks
hermes kanban reclaim <task_id>

# 2. Block to prevent re-dispatch (reclaim alone puts task back to ready)
hermes kanban block <task_id> "duplicate topic - reclaim of redundant task"

# 3. Kill zombie OS processes (reclaim releases kanban claim but not the process)
pid=$(ps aux | grep "$tid" | grep -v grep | awk '{print $2}' | head -1)
kill -9 $pid
```

**Critical**: `hermes kanban reclaim` releases the kanban worker claim but does NOT kill the OS process. The process continues running independently. Without blocking, the dispatcher re-dispatches the task on the next tick. Without killing, the zombie process consumes resources.

## batch_create_tasks.py Mitigation

The script's 35% thread diversity cap (set via `--count N`) naturally prevents topic monopolization WITHIN a single batch creation call:
- After cleanup of 12 duplicates, the script created 12 new tasks capped at 4/thread
- Thread distribution: injection(4), calibration(2), tfidf(2), ensemble(1), transfer(1), lr_detection(1), attack(1)
- No single topic exceeded 33% of created tasks

**⚠️ Diversity cap does NOT prevent cross-cycle duplicates.** The cap limits how many tasks per thread are created in ONE batch call, but it does NOT check if a topic is already running from a PREVIOUS cycle. In Cycle #603, the batch creator created 4 tasks that overlapped with existing running topics — the diversity cap had no effect because each task was from a different thread. See `references/batch-creator-cross-cycle-duplication.md` for the full pattern and the normalized topic comparison fix.

**Rule**: After manual duplicate cleanup, verify batch-created tasks against running topics BEFORE dispatch. The diversity cap handles within-batch monopolization; topic dedup detection handles cross-cycle duplicates.

## Key Insight

Topic-based duplicates are the most wasteful form of duplicate dispatch because:
1. ID-based dedup misses them entirely
2. Each duplicate consumes a full worker slot
3. Multiple workers run identical experiments, wasting API credits
4. Synthesis must merge redundant results

The batch creator's diversity cap is the primary prevention mechanism. Manual detection via topic grouping is the fallback detection mechanism.
