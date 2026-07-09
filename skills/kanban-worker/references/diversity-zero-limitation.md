# Curiosity Scorer Diversity=0 Limitation (added 2026-06-02)

## Problem

The `diversity` field in the curiosity scorer's `--json` output returns 0 for ALL items, even when threads have vastly different exploration levels. This makes the diversity component useless for automatic thread balancing.

## Observed Data (2026-06-02)

Queue had 28 items across 6 threads:
- `other`: 670 thread_count (massively overexplored)
- `injection`: 249 thread_count
- `domain`: 97 thread_count
- `attack`: 59 thread_count
- `calibration`: 62 thread_count
- `embedding`: 16 thread_count (underexplored)

Despite `embedding` having only 16 items vs `other` with 670, ALL items returned `diversity: 0`.

## Impact

The Director must enforce the 35% thread diversity cap manually. The scorer's diversity field provides no signal.

## Workaround

Use the `thread` and `thread_count` fields from the scorer output directly:

```python
from collections import Counter

# Get thread distribution from scorer output
thread_counts = Counter(item['thread'] for item in scored if not item.get('resolved'))

# Before creating each task, check if adding it would exceed 35%
def check_diversity(created_threads, candidate_thread, max_pct=0.35):
    total = len(created_threads)
    if total == 0:
        return False
    counts = Counter(created_threads)
    projected = (counts.get(candidate_thread, 0) + 1) / (total + 1)
    return projected > max_pct
```

## Detection

If every scored item has `diversity: 0`, the field is non-functional for this queue state. Switch to manual enforcement using `thread` field values.

## Related

- See `references/thread-diversity-enforcement.md` for the full enforcement pattern
- See `references/curiosity-scorer-json-format.md` for the scorer output schema
