# Queue Item Format Inconsistency

## Problem

When iterating over `self_state.json` → `curiosity_queue`, items may be either dicts or bare strings. Code that assumes dicts crashes with `AttributeError`.

## Example Failure

```python
# CRASHES when item is a string
for item in queue:
    text = item.get('text', str(item))  # AttributeError: 'str' object has no attribute 'get'
```

## Safe Pattern

```python
for item in queue:
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    priority = item.get('priority', '?') if isinstance(item, dict) else '?'
```

## Where This Hits

- Queue classification code (RESOLVED/RUNNING/DONE/NO_SOURCE)
- Coverage scoring (keyword overlap with running tasks)
- Queue triage during saturation
- Queue curation (tagging resolved items)
- Director queue curiosity pitfall detection

## Why It Happens

The `curiosity_queue` array is written by different synthesis workers across cycles. Some write dicts with structured fields (`text`, `priority`, `source`); others write bare strings. The format has been inconsistent since at least Cycle #150.

## Fix

ALWAYS use the `isinstance(item, dict)` guard before calling `.get()` on queue items. This applies to ALL queue iteration code, not just the first place you encounter it.
