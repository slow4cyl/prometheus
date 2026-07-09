# Batch Creator Score Threshold Gap

**Added:** Cycle #247
**Severity:** Medium
**Category:** Director workflow, queue triage

## Problem

The `batch_create_tasks.py` script filters queue items by a minimum score threshold (default: 60). Items scoring 58-59 can still be genuinely uncovered by running tasks and worth creating experiments for.

## Observed Behavior (Cycle #247)

- Batch creator found 3 items scoring ≥60, created 1 task (only 1 uncovered after coverage check)
- Manual coverage analysis revealed 1 additional genuinely open item at score 58 (C-extension scipy bottleneck)
- The item had coverage score 0.09 against running tasks — clearly uncovered
- Batch script filtered it out due to score threshold

## Detection

When the batch creator creates fewer tasks than free workers available:
1. Check how many free workers exist vs tasks created
2. If gap ≥2, run manual coverage analysis on items scoring 55-59
3. Use the queue classification code from kanban-worker skill to check coverage

## Fix Options

1. **Lower threshold temporarily:** `python3 ~/.hermes/scripts/batch_create_tasks.py --count 15 --min-score 55`
2. **Manual creation:** Create tasks individually for items confirmed uncovered by coverage scoring
3. **Post-batch supplementation:** After batch creates tasks, check remaining free workers against high-score uncovered items

## Coverage Analysis Code

```python
import json, re, os

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])
running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '').lower() for t in running]

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    words = set(re.findall(r'\b\w{4,}\b', text.lower()))
    stop = {'the', 'that', 'this', 'with', 'from', 'have', 'been', 'were', 'does', 'what', 'when', 'which', 'will', 'would', 'could', 'should', 'about', 'into', 'than', 'also', 'just', 'more', 'some', 'very', 'other'}
    keywords = words - stop
    running_text = ' '.join(running_titles)
    overlap = sum(1 for w in keywords if w in running_text)
    coverage = overlap / max(len(keywords), 1)
    status = "COVERED" if coverage > 0.2 else ("PARTIAL" if coverage > 0.1 else "OPEN")
    if status == "OPEN":
        print(f"[{i}] OPEN (cov={coverage:.2f}) {text[:100]}")
```

## Key Insight

The batch creator's score threshold is a heuristic for queue quality, not a filter for coverage. An item can score well on novelty/diversity but still be covered by running tasks, or score moderately but be genuinely uncovered. Coverage analysis (keyword overlap with running titles) is the ground truth for "does this need a new task?"
