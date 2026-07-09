# Queue Item Text JSON Wrapper Variant

## Problem

Some queue items have a `text` field that contains raw JSON instead of clean text. The item itself is a valid dict, so the mixed-format crash doesn't trigger — but the text content is corrupted:

```json
{"text": "{\"text\": \"Can raw ECE thresholding replace meta-classifiers\", \"source\": \"exp_2940\"}", "priority": "medium", "source": "exp_2940"}
```

When used as a task title via `hermes kanban create`, this produces:
```
exp_3328: {"text": "Can raw ECE thresholding replace meta-cl
```

## How It Differs From Other Queue Issues

| Issue | Detection | Crash? |
|-------|-----------|--------|
| Mixed dict/string (`batch-create-mixed-queue-format`) | `AttributeError: 'str' has no attribute 'get'` | Yes |
| Nested corruption (`queue-corruption-detection`) | `'text'` in text and `{\\'` in text | No (silent) |
| **JSON wrapper in text field** (this) | `{"text":` prefix in task title | No (corrupted title) |

## Detection

After creating tasks, check for corrupted titles:
```python
import sqlite3
conn = sqlite3.connect('~/.hermes/kanban.db')
corrupted = conn.execute("""
    SELECT id, title FROM tasks 
    WHERE status='running' AND (title LIKE '%"text":%' OR title LIKE '%\\"%')
""").fetchall()
for task_id, title in corrupted:
    print(f"CORRUPTED: {task_id}: {title[:70]}")
```

## Fix

Clean before using as title:
```python
import re, json

def clean_queue_text(text):
    if text.startswith('{"text":'):
        try:
            return json.loads(text).get('text', text)
        except json.JSONDecodeError:
            pass
    text = text.replace('\\"', '"')
    text = re.sub(r'^\{.*?"text":\s*"', '', text)
    text = re.sub(r'",\s*"source".*?\}$', '', text)
    return text.strip()
```

## Prevention

The `normalize_queue.py` script should handle this variant. Task creation scripts (both batch and manual) should apply `clean_queue_text()` before using item text as titles.

## Session Reference

- **Cycle**: #246 (June 3, 2026)
- **Impact**: 3 tasks created with corrupted titles (exp_3328, exp_3490, exp_3491)
- **Resolution**: Reclaimed corrupted tasks, cleaned titles manually
