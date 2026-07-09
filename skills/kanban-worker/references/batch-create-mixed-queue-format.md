# batch_create_tasks.py Mixed Queue Format Crash

## Problem
The batch task creation script crashes when the curiosity queue in `self_state.json` contains mixed item types — some strings, some dicts.

## Error
```
ERROR: Could not score queue: 'int' object has no attribute 'get'
Queue: 0 active items, 0 score >= 30
```

## Root Cause
The curiosity queue has accumulated items in different formats across synthesis cycles:
- **String items** (35 of 40): `"[NEW from synthesis v620] Ensemble size scaling — ..."`
- **Dict items** (5 of 40): `{"text": "[NEW from synthesis v608] Vocab overlap...", "source": "exp_2156"}`

The script calls `item.get('text', str(item))` on each item. When `item` is a string, Python's string type doesn't have a `.get()` method, causing the AttributeError.

## Detection
```bash
python3 ~/.hermes/scripts/batch_create_tasks.py --count 11 --dry-run --min-score 30
# Output: ERROR: Could not score queue: 'int' object has no attribute 'get'
```

## Fix
### Option A: Normalize queue before running script
```python
import json, os
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
q = ss.get('curiosity_queue', [])
normalized = []
for item in q:
    if isinstance(item, str):
        normalized.append({'text': item, 'source': 'unknown'})
    elif isinstance(item, dict):
        normalized.append(item)
    else:
        normalized.append({'text': str(item), 'source': 'unknown'})
ss['curiosity_queue'] = normalized
with open(os.path.expanduser('~/.hermes/self_state.json'), 'w') as f:
    json.dump(ss, f, indent=2, ensure_ascii=False)
```

### Option B: Fall back to manual task creation
When the batch script fails, create tasks individually:
```bash
hermes kanban create "exp_NNN: <title>" --assignee prometheus-worker-N --body "<body>"
```

## Prevention
The synthesis worker should normalize all queue items to dicts with `text` and `source` keys when appending to `self_state.json`'s `curiosity_queue`. The inconsistency originates from different synthesis versions using different formats.

## Session Reference
- **Cycle**: #244 (June 3, 2026)
- **Impact**: Batch creator produced 0 tasks despite 13 free workers
- **Workaround**: Created 11 tasks manually via individual `hermes kanban create` calls
- **Board state after fix**: 21 running tasks, all dispatched successfully
