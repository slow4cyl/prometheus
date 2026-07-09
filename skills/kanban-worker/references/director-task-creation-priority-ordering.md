# Director Task Creation Priority Ordering (added Cycle #222)

## Problem

After queue classification (RESOLVED/RUNNING/DONE/NO_SOURCE/UNKNOWN) and coverage scoring, the Director must decide WHICH items to create tasks for. The existing guidance says "create for NO_SOURCE items" but doesn't specify the full priority ordering or address DONE/ACTIVE items as task candidates.

In Cycle #222, coverage scoring filtered 13/14 NO_SOURCE items as COVERED (keyword overlap ≥0.15 with running tasks), leaving only 1 genuinely OPEN item. But the Director also had 11 DONE items, 2 of which were ACTIVE (items 13 and 16) with genuinely open questions not addressed by any running task. Focusing exclusively on NO_SOURCE missed these opportunities.

## Priority Order

After classifying queue items and running coverage scoring, create tasks in this order:

1. **NO_SOURCE items with LOW coverage** (<0.15) — genuinely uncovered research questions, always create
2. **DONE/ACTIVE items** — source experiment completed but question remains open (tagged `[ACTIVE — partial]`). These are high-value because the source data exists but the specific question wasn't answered. Verify the source experiment's findings don't already address the question before creating a task.
3. **DONE/UNTAGGED items** — source completed, no ACTIVE tag. Lower priority: the question may have been implicitly answered. Check the source experiment's summary before creating.
4. **NO_SOURCE items with MED/HIGH coverage** (≥0.15) — partially covered by running tasks. Only create if the running task addresses a DIFFERENT aspect of the same topic (keyword overlap doesn't guarantee same research question). Use judgment.
5. **RUNNING items** — NEVER create tasks. The source experiment will resolve these naturally.

## Key Insight

Keyword overlap (coverage score) is necessary but not sufficient for determining if a topic is covered. A queue item about "isotonic power law diminishing returns" may share the keyword "isotonic" with a running task about "isotonic vs Platt comparison" — but they investigate different aspects. The coverage score flags potential overlap; the Director must verify whether the running task actually addresses the queue item's specific question.

Conversely, DONE/ACTIVE items have a completed source experiment but the question remains explicitly open (the `[ACTIVE — partial]` tag was added by a prior synthesis or Director pass). These are almost always worth creating tasks for, because someone already determined the source experiment didn't fully answer the question.

## Detection Pattern

```python
# After classification, identify DONE/ACTIVE items
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    source_ids = re.findall(r'exp_(\d+\w*)', text)
    if source_ids and any(f'exp_{sid}' in ss_exp_ids for sid in source_ids):
        if 'ACTIVE' in text:
            # High priority — question explicitly open
            print(f"[{i}] DONE/ACTIVE: {text[:100]}")
        else:
            # Lower priority — may be implicitly answered
            print(f"[{i}] DONE/UNTAGGED: {text[:100]}")
```

## Integration with Existing Workflow

This priority ordering integrates with the Director Quick-Pass Flowchart (Cycle #186) at Step 6 (CREATE EXPERIMENT TASKS). Replace the existing priority order:

**Old:** "a) NO_SOURCE items, b) DONE/LOW-coverage items, c) DONE/MED-coverage items"
**New:** "a) NO_SOURCE/LOW + DONE/ACTIVE, b) DONE/UNTAGGED, c) NO_SOURCE/MED (if different aspect), d) never RUNNING"
