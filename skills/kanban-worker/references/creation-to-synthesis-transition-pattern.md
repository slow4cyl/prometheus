# Creation-to-Synthesis Transition Pattern (Cycle #246)

## When to Switch from Creation to Synthesis

The Director should switch from creating experiment tasks to creating synthesis tasks when:

1. **All queue items are covered** — manual overlap check at 0.45 threshold shows 0 uncovered items
2. **Free workers exist** — idle workers = wasted capacity
3. **Unsynthesized experiments exist** — prometheus.db has completed experiments not yet in synthesis_outputs

## Decision Flow

```
Queue has uncovered items?
  YES → Create experiment tasks (CREATION OVER SYNTHESIS)
  NO  → All workers busy?
          YES → Wait for completions
          NO  → Create synthesis tasks for unsynthesized experiments
```

## Worked Example (Cycle #246)

### Phase 1: Experiment Creation
- Batch creator found 3 uncovered items (0.6 threshold) → created 3 tasks
- Manual analysis at 0.45 threshold found 28 uncovered items → created 27 tasks
- All 20 free workers filled

### Phase 2: Queue Saturated
- Re-check: 0 uncovered items at 0.45 threshold (all queue items now covered)
- 14 workers free after some completions

### Phase 3: Synthesis Dispatch
- 2,912 unsynthesized experiments (filtered from 5,197 raw, minus 2,285 self-identified duplicates)
- Created 21 synthesis tasks (10 experiments each, most recent first)
- Each synthesis task covers exp_8898 → exp_3961

### Final State
- 50/50 workers busy (29 experiment + 21 synthesis)
- All queue items covered
- Synthesis backlog being actively reduced

## Key Metrics

| Metric | Value |
|--------|-------|
| Queue items | 50 (at cap) |
| Uncovered at 0.6 threshold | 3 |
| Uncovered at 0.45 threshold | 28 |
| Experiment tasks created | 27 + 3 = 30 |
| Synthesis tasks created | 21 |
| Total workers busy | 50/50 |
| Unsolved experiments (raw) | 5,197 |
| Self-identified duplicates | 2,285 (44%) |
| Unsolved experiments (filtered) | 2,912 |
| Experiments covered by synthesis | ~210 (21 tasks × 10) |

## Why Manual Fallback Matters

The batch creator's 0.6 Jaccard threshold treats items as "covered" when they share 60%+ vocabulary with running tasks. But in domain-saturated research (all items about injection/calibration/detection), virtually every item shares vocabulary with running tasks. The 0.45 threshold catches items that share vocabulary but test genuinely different hypotheses.

Example: "Does ECE thresholding detect hallucination?" vs "Does ECE thresholding hold for text adversarial?" — both contain "ECE thresholding" but investigate completely different transfer targets (hallucination vs jailbreaks). At 0.6 overlap, the second is marked "covered." At 0.45, it's correctly identified as a distinct investigation.
