# Manual Task Creation Fallback

When `batch_create_tasks.py` crashes (experiments=int, mixed queue types, or any error), use this workflow to manually create tasks for free workers.

## Quick Steps

1. **Classify queue items** — see `kanban-worker` SKILL.md "Queue item classification taxonomy"
2. **Score coverage** — see "Quantitative coverage scoring" section
3. **Pick lowest-coverage NO_SOURCE items** — pure research questions not addressed by running experiments
4. **Create manually** with experiment task body template

## Decision Rules

- **NO_SOURCE + LOW coverage (<0.15)**: Always create task
- **NO_SOURCE + MED coverage (0.15-0.30)**: Create if free workers > 3
- **DONE + LOW coverage**: Create if the source experiment didn't answer the queue item's specific question
- **RUNNING**: Never create — item will resolve when running experiment completes
- **RESOLVED**: Never create — already tagged as resolved

## Example (from Cycle #584)

Board: 16 running tasks, 11 free workers, batch creator crashed.

1. Classified 42 queue items → 29 NO_SOURCE, 3 DONE, 1 RUNNING, 9 RESOLVED
2. Scored coverage: all items ≥0.17 (partial overlap with running tasks)
3. Picked 5 lowest-coverage items (0.17-0.29)
4. Created exp_2304-2308 manually with queue item text as hypothesis
5. All 5 dispatched successfully on next scheduler tick

## Coverage Thresholds (updated Cycle #246)

The batch creator uses 0.45 overlap (Jaccard word overlap) as its coverage threshold.
Items below 0.45 can be genuinely distinct — they share vocabulary but test different hypotheses.

| Coverage | Action |
|----------|--------|
| <0.15 | Always create |
| 0.15-0.30 | Create if workers available |
| 0.30-0.45 | Create if workers available (often genuinely distinct) |
| 0.45-0.60 | Verify topic before creating (may share vocabulary but differ in hypothesis) |
| >0.60 | Skip (likely same investigation) |

**Key insight (Cycle #246):** Items with overlap 0.3-0.45 are the sweet spot for manual creation.
They share enough vocabulary to be in the same research domain, but test different hypotheses
(e.g., "Does X generalize to audio?" vs "Does X apply to code?" — both contain "generalize"
but investigate completely different transfer targets). The batch creator at 0.6 threshold
misses all of these, leaving workers idle.
