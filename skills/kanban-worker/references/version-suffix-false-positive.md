# Version Suffix False Positive in Duplicate Detection

**Added**: Cycle #219
**Category**: Director workflow, duplicate detection
**Severity**: Low (cosmetic, wastes diagnostic time)

## Problem

Tasks with version suffixes (exp_811_v2, exp_438c) are flagged as duplicates of their base experiment (exp_811, exp_438) by the ID-based intersection check in the Director's duplicate detection routine.

## Example

```
RUNNING: t_d3309ef5 - exp_811_v2: Calibration scaling law — cross-validation fix
DONE: t_2e70103c - exp_811: Isotonic regression scaling law generalization — calibration methods
```

The duplicate check extracts `exp_811` from both titles and flags them as duplicates. But `exp_811_v2` is an intentional follow-up experiment, not a duplicate.

## Root Cause

The regex `exp_(\d+)` extracts only the numeric portion, losing the version suffix. Both `exp_811` and `exp_811_v2` normalize to `exp_811`.

## Fix

When the duplicate check finds a match, check if the running task's title contains `_v2`, `_v3`, or a letter suffix (e.g., `438c`). If so, it's a follow-up, not a duplicate.

```python
import re

def is_versioned(title):
    """Check if task title has a version suffix."""
    return bool(re.search(r'exp_\d+_[vV]?\d+[a-z]?', title))

def get_base_exp_id(title):
    """Extract base experiment ID without version suffix."""
    m = re.search(r'exp_(\d+)', title)
    return "exp_%s" % m.group(1) if m else None

# In duplicate detection:
for t in running:
    title = t.get('title', '')
    if is_versioned(title):
        continue  # Skip versioned tasks — they're follow-ups, not duplicates
    # ... rest of duplicate check
```

## Prevention

The Director's duplicate detection should:
1. Extract experiment IDs from task titles
2. Filter out versioned tasks (those with `_v2`, `_v3`, letter suffixes)
3. Only check non-versioned tasks for duplicates

## Related

- See `Duplicate dispatch — same experiment in done + running` pitfall in main SKILL.md
- See `same-topic different-ID duplicates` pitfall for cross-ID topic duplicates
