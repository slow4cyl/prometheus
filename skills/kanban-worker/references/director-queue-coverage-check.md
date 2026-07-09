# Director Queue Coverage Check (added Cycle #239)

## The Problem

The Director flowchart checks saturation (≥7 running → synthesis-only) but doesn't check whether queue items are already covered by running experiments. This leads to creating tasks for items that will resolve naturally when their covering experiment completes.

## The Pattern

After classifying queue items (step 4) and before checking saturation (step 5), run a quantitative coverage check:

```python
import json, re

running = json.load(open('/tmp/kanban_running.json'))
running_titles = ' '.join(t.get('title', '').lower() for t in running)

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'does', 'this', 'that',
        'more', 'most', 'some', 'such', 'just', 'over', 'also', 'even', 'still'}

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    words = set(w.lower() for w in re.findall(r'\w+', text) if len(w) > 3)
    keywords = words - stop
    overlap = sum(1 for w in keywords if w in running_titles)
    coverage = overlap / max(len(keywords), 1)
    status = 'HIGH' if coverage > 0.3 else ('MED' if coverage > 0.15 else 'LOW')
    print(f'[{i:2d}] ({status} cov={coverage:.2f}) {text[:80]}')
```

## Decision Rule

- **HIGH coverage (>0.3)**: Item is covered by running experiments. Do NOT create a task.
- **MED coverage (0.15-0.3)**: Partially covered. Lower priority for task creation.
- **LOW coverage (<0.15)**: Genuinely uncovered. Candidate for new task.

## Integration with Director Flowchart

Insert as step 5b between "CHECK SATURATION" and "CREATE EXPERIMENT TASKS":

```
5b. CHECK QUEUE COVERAGE (even when below saturation)
    If ALL active queue items have HIGH coverage → NO NEW TASKS regardless of
    free worker count. Free workers are picked up by the next dispatch tick.
```

## When This Matters Most

- Queue has 15+ items but all are follow-ups from currently running experiments
- Below saturation threshold (e.g., 5 running, 15 free workers) but queue is fully in-flight
- After a burst of task creation where queue was large but most items were already covered

## Real-World Example (Cycle #239)

Board had 12 running tasks, 15 free workers, 21 queue items. Queue classification showed all 15 active items were either:
- Source experiments DONE but covered by running experiments (exp_1159-1169)
- Source experiments RUNNING (exp_1153, exp_1126, exp_1123)

Result: 0 new tasks created despite 15 free workers. All queue items were already in-flight.
