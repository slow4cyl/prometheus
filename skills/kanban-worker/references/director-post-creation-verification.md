# Director Post-Creation Verification Pattern

Added: Cycle #214

## Problem

When the Director creates tasks via a pre-existing script (from a previous session), the script may have stale context and create tasks that duplicate already-running experiments. Step 4 (CLASSIFY QUEUE ITEMS) correctly identifies duplicates before creation, but there's no explicit post-creation verification between CREATE (step 6) and DISPATCH (step 7).

## Observed in Cycle #214

A task-creation script from a previous session produced 6 tasks, 4 of which duplicated existing running experiments:
- exp_778 duplicated exp_752/766 (auto-generate domain gates)
- exp_779 duplicated exp_767 (framing selector)
- exp_780 duplicated exp_768 (anti-sycophancy framing)
- exp_782 duplicated exp_775 (numerical specificity)

These were caught and resolved via reclaim+block, but the waste was avoidable.

## Fix: Add Step 6b to Director Flowchart

After step 6 (CREATE EXPERIMENT TASKS), add:

```
6b. VERIFY CREATED TASKS (after bulk creation via script)
   After creating tasks (especially from pre-existing scripts), check each new task's
   title/topic against running task titles to catch duplicates from stale scripts or
   misclassification. If duplicates found: reclaim + block immediately (see "Duplicate
   dispatch" pitfall). This catches cases where a script from a previous session has
   stale context and creates tasks that overlap with already-running experiments.
```

## Quick Verification Pattern

After creating tasks in bulk, run:

```python
import json, re

# Load running tasks
running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '').lower() for t in running]

# Load newly created tasks (from dispatch output or kanban list --status ready)
new_tasks = [...]  # titles of tasks just created

for title in new_tasks:
    title_lower = title.lower()
    # Check for significant keyword overlap with running tasks
    words = set(title_lower.split())
    for running_title in running_titles:
        running_words = set(running_title.split())
        overlap = len(words.intersection(running_words))
        if overlap >= 3:  # 3+ shared words = likely duplicate
            print(f"DUPLICATE: {title[:60]} overlaps with {running_title[:60]}")
```

## Prevention

1. Always regenerate task lists from current state — don't reuse cached scripts
2. Include a dedup check in task-creation scripts
3. After creation, verify against `kanban list --status running` before dispatching

## Related Pitfalls

- "Duplicate dispatch — same experiment in done + running" (Cycle #159)
- "Same-topic different-ID duplicates" (Cycle #185)
- "Reclaimed duplicates get re-dispatched" (Cycle #185)
