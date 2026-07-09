# Queue Corruption Detection (Cycle #249)

## Problem

Synthesis workers appending to already-corrupted queue entries produce nested JSON patterns:
```
{'text': '{\\'text\\': \\'{\\\\\\'text\\\\\\': \\\\\\'{...
```

In Cycle #249, 55% of the 3964-item queue was corrupted (2187 items). The `batch_create_tasks.py` script found 871 "uncovered" items, but most were garbage from the corruption pattern.

## Detection

```python
def is_corrupted(text):
    """Detect nested 'text' corruption from synthesis worker appends."""
    return isinstance(text, str) and "'text'" in text and "{\\'" in text
```

Quick stats from Cycle #249:
- Total queue: 3964 items
- Corrupted: 2187 (55%)
- Valid: 1777 (45%)

## Impact on Director Decisions

1. **batch_create_tasks.py will create tasks from corrupted items** — the script doesn't filter corruption, so it picks up garbage topics as "uncovered"
2. **Creating tasks from corrupted items wastes worker slots** — workers get assigned nonsensical topics
3. **The CORRUPTED category should be added to the queue classification taxonomy** — see kanban-worker skill

## Decision Rule

When queue corruption > 30%:
- Do NOT create tasks even if workers are free
- Ensure a curation task is running to clean the queue
- Wait for curation to complete before creating tasks
- The curation task should filter out corrupted entries and keep only valid items

## Curation Task Pattern

```
hermes kanban create "QUEUE CURATION: deduplicate and trim N-item queue to 50 items" \
  --assignee prometheus-worker-N \
  --body "Deduplicate and clean the curiosity queue. Remove:
1. Corrupted entries (nested 'text' patterns)
2. Items tagged [RESOLVED]
3. Duplicate items asking the same question
Keep ~50 high-priority valid items."
```

## Root Cause

Synthesis workers use `kanban_comment` to add queue items, but the comment body gets interpreted as nested JSON when the original item text contains quotes. Each synthesis cycle compounds the corruption.
