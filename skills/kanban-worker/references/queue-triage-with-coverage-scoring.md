# Queue Triage with Coverage Scoring — Director Pass Pattern

Proven in Cycle 245. Combines queue item classification with quantitative coverage scoring against running task titles to identify genuinely uncovered items for task creation.

## Why This Works

The queue classification (RESOLVED/RUNNING/DONE/NO_SOURCE) alone is insufficient — NO_SOURCE items can overlap topically with running experiments (e.g., "FPR reduction" queue item vs "script-aware gating FPR" running task). Coverage scoring catches these overlaps by measuring keyword overlap between queue items and running task titles.

## Step 1: Classify Queue Items

```python
import json, re, os

done = json.load(open('/tmp/kanban_done.json'))
running = json.load(open('/tmp/kanban_running.json'))

running_exp_ids = set()
for t in running:
    for m in re.finditer(r'exp_(\d+\w*)', t.get('title', '')):
        running_exp_ids.add(f'exp_{m.group(1)}')

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
exps = ss.get('experiments', {})
if isinstance(exps, dict):
    for e in exps.get('completed', []):
        if isinstance(e, dict):
            eid = e.get('id', '')
            if eid: ss_exp_ids.add(eid)
        elif isinstance(e, str):
            m = re.search(r'exp_(\\d+\\w*)', e)
            if m: ss_exp_ids.add(f'exp_{m.group(1)}')
else:
    # experiments is an integer count — actual list is in metrics.experiments_completed_list
    for e in ss.get('metrics', {}).get('experiments_completed_list', []):
        if isinstance(e, int):
            ss_exp_ids.add(f'exp_{e}')
        elif isinstance(e, str):
            m = re.search(r'exp_(\\d+)', e)
            if m: ss_exp_ids.add(f'exp_{m.group(1)}')

queue = ss.get('curiosity_queue', [])

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    source_ids = re.findall(r'exp_(\d+\w*)', text)
    if not source_ids:
        category = "NO_SOURCE"
    elif any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
        category = "RUNNING"
    elif any(f'exp_{sid}' in ss_exp_ids for sid in source_ids):
        category = "DONE"
    else:
        category = "UNKNOWN"
    # Only NO_SOURCE items are candidates for new tasks
```

## Step 2: Coverage Scoring for NO_SOURCE Items

Filter NO_SOURCE items by keyword overlap with running task titles:

```python
stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'we'}

running_titles_text = ' '.join(t.get('title', '').lower() for t in running)

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    source_ids = re.findall(r'exp_(\d+\w*)', text)
    if source_ids:
        continue  # Only score NO_SOURCE items
    words = set(re.findall(r'[a-z]+', text.lower()))
    keywords = {w for w in words - stop if len(w) > 2}
    overlap = sum(1 for w in keywords if w in running_titles_text)
    coverage = overlap / max(len(keywords), 1)
    # HIGH >0.3 (likely covered), MED 0.15-0.3 (partially), LOW <0.15 (open)
    if coverage < 0.15:
        print(f'[{i}] LOW coverage ({coverage:.2f}) — candidate for new task: {text[:80]}')
```

## Step 3: Create Tasks for LOW-Coverage Items

Filter to coverage < 0.15 (or < 0.20 for slightly broader), deduplicate by topic, assign to free workers.

## Deduplication Within Queue

The queue can contain near-duplicate items (same question rephrased). Before creating tasks, group by normalized topic:

```python
from collections import defaultdict
topics = defaultdict(list)
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    # Normalize: lowercase, strip exp_NNN prefixes, take first 50 chars
    normalized = re.sub(r'exp_\d+\w*', '', text.lower())[:50].strip()
    topics[normalized].append(i)
for norm, indices in topics.items():
    if len(indices) > 1:
        print(f'DUPLICATE: indices {indices} — {norm}')
```

Pick the highest-scored item from each duplicate group.

## Thread Diversity Cap

When creating N tasks, no single thread should exceed 35% of created tasks. The curiosity scorer's `thread` field provides thread labels. Example: creating 8 tasks across 6 threads is safe (max thread = 2 tasks = 25%).

## Cycle 245 Results

- Queue: 35 items (11 RESOLVED, 6 DONE, 15 NO_SOURCE, 3 UNKNOWN)
- After coverage scoring: 8 NO_SOURCE items with coverage < 0.20
- Created 8 tasks across 6 threads, all spawned successfully
- Zero wasted workers on topics already covered by running experiments
