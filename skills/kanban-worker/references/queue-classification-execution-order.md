# Queue Classification — Two-Phase Execution Order (Cycle #251)

## Problem

The kanban-worker skill documents two queue analysis techniques separately:
1. **Queue item classification taxonomy** (RESOLVED/RUNNING/DONE/NO_SOURCE/UNKNOWN) — checks source experiment status
2. **Quantitative coverage scoring** (HIGH/MED/LOW) — computes keyword overlap with running tasks

Running them as a single pass produces incorrect categories. In Cycle #251, a Director pass classified all 44 queue items as HIGH_COVERED (35) or MED_COVERED (9) — missing the RUNNING items entirely because the coverage check ran before source-status classification.

## Correct Execution Order

### Phase 1: Source Status Classification
Check each item's source experiment against running and completed experiment IDs.

```python
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text: continue
    
    source_ids = re.findall(r'exp_(\d+)\b', text)
    if source_ids and any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
        # RUNNING — source experiment still active, will resolve naturally
        continue
    elif source_ids and any(f'exp_{sid}' in ss_exp_ids for sid in source_ids):
        # DONE — source completed, check if question answered
        ...
    elif not source_ids:
        # NO_SOURCE — pure research question, always candidate
        ...
```

### Phase 2: Coverage Scoring (NON-RUNNING items only)
Only apply keyword overlap scoring to items that survived Phase 1 as non-RUNNING.

```python
    # Phase 2: coverage scoring (only for non-RUNNING items)
    words = set(re.findall(r'[a-z_]+', text.lower()))
    keywords = {w for w in words - stop if len(w) > 2 and not re.match(r'^exp_\d+$', w)}
    overlap = sum(1 for w in keywords if w in running_text)
    coverage = overlap / max(len(keywords), 1)
    # HIGH >0.3, MED 0.15-0.3, LOW <0.15
```

### Phase 3: Task Creation Decision
Create tasks ONLY for:
- LOW-coverage NO_SOURCE items (pure research, uncovered)
- DONE/ACTIVE items whose question is NOT fully answered

Skip: RUNNING items (will resolve naturally), HIGH/MED coverage items (effectively covered).

## Why This Matters

When Phase 2 runs before Phase 1, RUNNING items get scored by keyword overlap and may appear as HIGH_COVERED (because their topic keywords match running task titles — including themselves). This causes the Director to conclude the queue is "fully covered" and skip task creation, even though the RUNNING items are self-referential matches, not genuinely covered by other experiments.

The two-phase order ensures RUNNING items are filtered out by source-status first, before any coverage scoring runs.

## Combined Code Template

See the full combined code in the kanban-worker skill's "Quantitative coverage scoring" section — but always run source-status classification FIRST, then coverage scoring on the remainder.
