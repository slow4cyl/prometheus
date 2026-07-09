# completed_at Type Inconsistency (added 2026-06-02)

## Problem

While `completed_at` is documented as a Unix timestamp (int), some tasks return it as a string (e.g., `"2026-06-02T00:54:44Z"` instead of `1780207924`).

Code that does `int(t['completed_at'])` or `datetime.fromtimestamp(t['completed_at'])` will fail with `TypeError: an integer is required (got type str)`.

## Affected Code Patterns

```python
# BROKEN — fails when completed_at is a string
completed_at = t.get('completed_at', 0)
dt = datetime.datetime.fromtimestamp(completed_at, tz=datetime.timezone.utc)

# BROKEN — fails when completed_at is an int
completed_at = t.get('completed_at', 'N/A')
dt = datetime.fromisoformat(completed_at)

# BROKEN — fails when completed_at is a string
age = time.time() - t['completed_at']  # TypeError: unsupported operand type(s)
```

## Fix

Always coerce with a type check:

```python
val = t.get('completed_at', 0)
if isinstance(val, str) and 'T' in val:
    dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
elif val:
    dt = datetime.datetime.fromtimestamp(int(float(val)), tz=datetime.timezone.utc)
else:
    dt = None
```

For age calculation:

```python
val = t.get('completed_at', 0)
if isinstance(val, (int, float)):
    age_min = (time.time() - val) / 60
elif isinstance(val, str) and 'T' in val:
    dt = datetime.fromisoformat(val.replace('Z', '+00:00'))
    age_min = (time.time() - dt.timestamp()) / 60
else:
    age_min = 999
```

## When This Happens

The inconsistency appears to correlate with how the task was completed:
- Tasks completed via `kanban_complete` from inside a worker session → int (Unix timestamp)
- Tasks completed by some other mechanism (archival, cleanup) → string (ISO format)

## Related Pitfalls

- See main SKILL.md for the documented "`completed_at` is a Unix timestamp (int)" rule
- This pitfall extends that rule to handle the string variant
