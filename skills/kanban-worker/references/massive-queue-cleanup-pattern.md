# Massive Queue Cleanup Pattern (3220→50)

## When to Use

Queue exceeds 500 items with severe quality issues:
- 25%+ items already tagged RESOLVED
- Deeply nested dict structures (not clean text)
- 100+ questions duplicated 3+ times
- Queue is >10x the target size (50)

## Diagnosis

```python
import json, re, os
from collections import Counter

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

resolved = sum(1 for i in queue if 'RESOLVED' in str(i))
questions = [re.sub(r'\[.*?\]\s*', '', str(i.get('text', i)))[:80].lower() for i in queue]
dupes = sum(1 for q, c in Counter(questions).items() if c >= 3)

print(f"Queue: {len(queue)}, Resolved: {resolved}, Dupes: {dupes}")
print(f"Quality: {(len(queue) - resolved - dupes) / max(len(queue), 1) * 100:.0f}% clean")
```

## Cleanup Strategy

1. **Strip RESOLVED items** — remove all items tagged `[RESOLVED]`, `[RESOLVED by exp_NNN]`, `[RESOLVED exp_NNN]`
2. **Fix nested dicts** — many items are `{'text': "{'text': '[NEW from ...] ..."}` (3+ levels deep). Extract innermost text.
3. **Deduplicate by core question** — strip prefixes (`[NEW from synthesis v545]`, `[COVERED by running tasks]`), normalize, keep highest-scored instance
4. **Quality filter** — keep only items that:
   - Don't reference completed experiments (exp_NNN in self_state.json)
   - Aren't covered by running tasks
   - Are genuine unanswered research questions
5. **Target**: 30-50 high-quality items

## Execution

This is a **maintenance task**, not investigation. Create a dedicated curation task:

```bash
hermes kanban create "QUEUE REBUILD: Clean 3220→50 items" \
  --assignee prometheus-worker-N \
  --body "QUEUE REBUILD TASK: ...
1. Read self_state.json
2. Clean curiosity_queue: strip RESOLVED, fix nested dicts, deduplicate
3. Keep only genuine unanswered questions
4. Target: 30-50 items
5. Write self_state.json once
6. Log changes to self_audit.log"
```

## Pitfalls

- **Don't create curation tasks for workers already assigned by batch_create** — see double-assignment-pitfall-cycle247.md
- **Queue is a working FIFO, not an archive** — consumed curiosities aren't persisted
- **Deep nesting gets worse over time** — each synthesis cycle can add another layer of dict wrapping if the append pattern isn't clean

## Expected Outcome

- Queue: 3220 → 30-50 clean items
- Resolution: ~1260 RESOLVED items removed
- Deduplication: ~187 questions with 3+ copies consolidated
- Time: 5-10 minutes for the curation worker
