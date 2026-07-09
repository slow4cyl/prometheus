# completed_at Format Variants (added Cycle #252)

## Problem
The `completed_at` field in kanban JSON output can appear as EITHER:
- **Unix epoch integer** (e.g., `1780207923`) — from `kanban show --json`
- **ISO 8601 string** (e.g., `"2026-06-02T03:30:00"`) — from `kanban list --json`

`created_at` and `started_at` can also be either format.

## Symptom
```python
# FAILS when completed_at is ISO string
sorted(tasks, key=lambda x: int(x.get('completed_at', 0)))
# ValueError: invalid literal for int() with base 10: '2026-06-02T03:30:00'

# FAILS when completed_at is Unix int
datetime.datetime.fromisoformat(t['completed_at'])
# TypeError: fromisoformat: argument must be str
```

## Fix
Use a safe parser that handles both formats:
```python
import datetime

def parse_timestamp(ts):
    if isinstance(ts, (int, float)):
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    elif isinstance(ts, str):
        return datetime.datetime.fromisoformat(ts)
    return None

# Usage in sort
sorted(tasks, key=lambda x: parse_timestamp(x.get('completed_at')) or datetime.datetime.min)
```

## Context
- This is a known inconsistency in the kanban CLI — `list` and `show` endpoints serialize timestamps differently
- The pitfall says "completed_at is a Unix timestamp" but that's only true for `show`, not `list`
- Director batch scripts that sort completed tasks by recency hit this when processing `list --json` output
