# Synthesis Task Filtering False Positive

## Problem

When filtering running tasks to find actual synthesis tasks, naive substring matching on `'synth' in title.lower()` produces false positives because experiment task titles contain "synthesis" as a source reference.

### Example

All 22 running tasks matched when using:
```python
synth_tasks = [t for t in running if 'synth' in t.get('title', '').lower()]
```

Because every experiment task title looks like:
```
exp_2410: [NEW from synthesis v626] Cross-dataset few-shot adaptation — can 5-10...
```

The word "synthesis" appears in the source reference `[NEW from synthesis v626]`, not as a task type indicator.

## Fix

Check if the title **starts with** the synthesis keyword:

```python
# WRONG — matches ALL experiment tasks with "synthesis" in their source reference
synth_tasks = [t for t in running if 'synth' in t.get('title', '').lower()]

# CORRECT — only matches actual synthesis tasks
synth_tasks = [t for t in running if t.get('title', '').lower().strip().startswith(('synthesis', 'consolidate'))]
```

## Why This Matters

The count of running synthesis tasks determines whether to create a new synthesis task. A false positive of 22/22 makes it appear synthesis is already running, causing the Director to skip creating a needed synthesis task.

## The `is_synth` Check in ID Extraction Is Correct

The same substring pattern (`'synth' in title.lower()`) is used when extracting experiment IDs from done tasks to exclude synthesis tasks. There it's **correct** because you want to exclude ANY task mentioning synthesis — even experiment tasks that reference it as a source (their titles list experiment IDs as subjects, not as the task's own experiment).

```python
# This is CORRECT for excluding synthesis tasks from experiment ID extraction
is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
```

The distinction: when FINDING synthesis tasks, use startswith. When EXCLUDING synthesis tasks from ID extraction, substring match is fine.
