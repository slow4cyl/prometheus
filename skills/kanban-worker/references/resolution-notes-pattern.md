# Resolution Notes Pattern for Synthesis Tasks

**Added**: Cycle #219
**Category**: Director workflow, synthesis task management
**Severity**: Medium (improves synthesis coverage accuracy)

## Problem

When the Director discovers that queue items have been resolved by recent experiments, those items should be tagged `[RESOLVED by exp_XXX]` in the queue. However, the Director cannot write to self_state.json (only synthesis worker can). The Director needs to communicate resolution information to the synthesis worker.

## Solution

Add resolution notes to the synthesis task via `kanban_comment`, alongside the experiment summaries. This ensures the synthesis worker knows which queue items to tag as RESOLVED.

## Pattern

```python
# Step 1: Identify resolved items by checking self_state.json
# (Director reads self_state but does NOT write to it)

# Step 2: Add resolution notes to synthesis task
hermes kanban comment <synthesis_task_id> "QUEUE RESOLUTION NOTES FOR SYNTHESIS WORKER:

Items likely RESOLVED by recent experiments:
- Item [N]: 'queue item text' → RESOLVED by exp_XXX (brief finding). Tag as [RESOLVED by exp_XXX].

Items needing new tasks when saturation clears:
- Item [M]: 'queue item text' (coverage status)

The synthesis worker should tag resolved items and add 1-3 new curiosities from synthesis findings."
```

## Example (Cycle #219)

```bash
hermes kanban comment t_8862d79f "QUEUE RESOLUTION NOTES FOR SYNTHESIS WORKER:

Items likely RESOLVED by recent experiments:
- Item [19]: 'Domain-matched centroid detection fails' → RESOLVED by exp_845 (REFUTED: per-domain centroids WORSE). Tag as [RESOLVED by exp_845].
- Item [41]: 'Isotonic exponent -0.447 vs -0.316' → RESOLVED by exp_838 (CONFIRMED). Tag as [RESOLVED by exp_838].

Items needing new tasks when saturation clears:
- Item [13]: 'Erosion mechanism requires BOTH coherent facts' (LOW coverage)
- Item [34]: 'Block formats lower success' (LOW coverage)
- Item [40]: 'Non-monotonic compression' (LOW coverage)"
```

## Benefits

1. Synthesis worker gets explicit resolution targets (no guessing)
2. Director maintains single-writer invariant (doesn't write to self_state.json)
3. Queue curation is deterministic (synthesis worker tags exact items)
4. Low-coverage items are identified for future task creation

## When to Use

- After identifying resolved queue items during Director pass
- When adding experiments to running synthesis task
- When synthesis task is <10 minutes old (before worker starts processing)

## When NOT to Use

- When synthesis task is >10 minutes old (worker may have already started — create new synthesis task instead)
- When queue items are ambiguous (let synthesis worker decide)

## Related

- See `Queue item resolution technique (added Cycle #151)` in main SKILL.md
- See `Comment race condition — synthesis completes before comment arrives (added Cycle #188)` pitfall
- See `Expanding scope on a running synthesis task (added Cycle #159)` pattern
