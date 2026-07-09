# Synthesis Task Deduplication — Director Quick-Pass Refinement

**Added:** Cycle #260 (2026-06-02)
**Applies to:** Director quick-pass flowchart, step 3 (DETECT UNSYNTHESIZED)

## The Gap

The flowchart says:
```
If ≥3 unsynthesized → CREATE SYNTHESIS TASK (always, regardless of saturation)
```

This doesn't account for synthesis tasks that are already running and covering most of the unsynthesized experiments. In Cycle #260, 6 experiments were unsynthesized but 5 were already covered by a running synthesis task (t_5f0700bb, dispatched 2 min earlier). Creating a second synthesis task for 1 remaining experiment (exp_1398) would be wasteful.

## The Fix

Before creating a synthesis task, check for existing ones:

```bash
hermes kanban list --status running --status ready --json > /tmp/kanban_running_ready.json
```

Then filter by title containing 'synth' or 'consolidat'. If a synthesis task already exists:

- **Covers most unsynthesized experiments** → Note uncovered experiments for next pass. Don't create a duplicate.
- **Covers few or none** → Create a new synthesis task, or add uncovered experiments via `kanban_comment` (if task is recent, <5 min).
- **Covers none and task is old** → Create a new synthesis task.

## Decision Matrix

| Unsynthesized count | Existing synthesis covers | Action |
|---------------------|---------------------------|--------|
| ≥3 | None/mostly none | CREATE new synthesis task |
| ≥3 | Most (>50%) | NOTE uncovered for next pass |
| ≥3 | All | SKIP (already handled) |
| 1-2 | None | NOTE for next pass (don't waste a worker on 1-2 experiments) |
| 1-2 | Some | NOTE uncovered remainder for next pass |
| <3 | Any | SKIP |

## Why Not kanban_comment?

The "Late-completing experiments" pattern (Cycle #147) says to add experiments via `kanban_comment`. But the "Revised rule (Cycle #246)" says NOT to expand scope in the same pass. When only 1-2 experiments are uncovered and the synthesis task is recent, it's better to wait than to risk the comment race condition.

The synthesis worker completes in <2 minutes — a comment added after dispatch may arrive after completion.

## Related Pitfalls

- `references/director-synth-comment-race-prevention.md` — comment race condition
- `references/duplicate-synthesis-task.md` — full prevention pattern (Cycle #161/162)
- `references/late-completing-experiments.md` — kanban_comment pattern (Cycle #147)
