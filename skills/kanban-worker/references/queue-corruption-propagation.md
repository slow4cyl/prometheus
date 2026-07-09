# Queue Corruption → Task Title Propagation (added Cycle #244)

## Problem

The curiosity queue in `self_state.json` can accumulate corrupted entries where
items are serialized dicts instead of plain strings. When `batch_create_tasks.py`
reads these corrupted items, it creates kanban tasks with corrupted titles and
bodies. Workers spawned from these tasks receive garbled hypothesis text.

## Corruption Pattern

Corrupted queue items look like nested JSON escaping:

```
{'text': '{\\'text\\': \\'{\\\\\\'text\\\\\\': ...'}
```

Instead of clean items:

```
{'text': '[NEW from exp_1285] Transfer learning sweet spot is n=5', 'priority': 'high'}
```

## Cascade

1. Synthesis worker writes `str(dict_item)` or `json.dumps(item)` instead of `item['text']`
2. Queue accumulates corrupted entries (observed: 3964 items, 89% corrupted in Cycle #244)
3. `batch_create_tasks.py` reads corrupted text → creates tasks with corrupted titles
4. Workers get garbled input → may produce meaningless experiments or waste API budget
5. Running tasks show `{'text':` or `\\\\` in their titles

## Detection

```bash
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json
python3 -c "
import json
d = json.load(open('/tmp/kanban_running.json'))
corrupt = sum(1 for t in d if \"{'text':\" in t.get('title','') or '\\\\\\\\' in t.get('title',''))
print(f'Running: {len(d)}, Corrupt titles: {corrupt}')
"
```

## Fix

Create a curation task:

```bash
hermes kanban create "QUEUE CURATION: repair corrupted queue" \
  --assignee prometheus-worker-N \
  --body "The curiosity queue has N items, M% corrupted. Unwrap all nested
  JSON/dict structures, deduplicate by normalized text, tag resolved items,
  trim to 30-50 clean items. Write back via Python file I/O."
```

## Prevention

Synthesis workers MUST write queue items as plain strings:

```python
# CORRECT — plain string
queue.append({"text": "[NEW from exp_XXX] hypothesis text here", "priority": "high"})

# WRONG — serialized dict (causes nesting)
queue.append(str({"text": "..."}))
queue.append(json.dumps({"text": "..."}))
```

The queue array should contain objects where `text` is always a plain string,
never nested JSON or dict representation.
