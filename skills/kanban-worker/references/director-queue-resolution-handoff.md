# Director Queue Item Resolution Handoff

**Added:** Cycle #215
**Category:** Director-Synthesis communication gap

## Problem

When the Director classifies queue items during triage, it identifies items whose source experiments have completed and whose questions are answered. These should be tagged `[RESOLVED by exp_XXX]` in `self_state.json`. But the Director cannot write to `self_state.json` (single-writer invariant). If the Director doesn't tell the synthesis worker which items to tag, the items remain untagged indefinitely — they accumulate as "active" items that will never be addressed because their questions are already answered.

## Observed in Cycle #215

During a Director pass, items 31 ("theoretical minimum ECE") and 41 ("contradiction-only detection replacement") were classified as resolved:
- Item 31: exp_774 (done) answered the isotonic regression question
- Item 41: exp_773 (done) answered the contradiction-only detection question

Both source experiments were in `self_state.json`'s `experiments.completed`, but the queue items were NOT tagged RESOLVED. The Director identified this during triage but did not include the resolution list in the synthesis task body. The synthesis worker had no way to know which items needed tagging.

## Fix

When creating a synthesis task, include a **QUEUE RESOLUTION** section listing items that should be tagged RESOLVED:

```python
# In the synthesis task body, add:
QUEUE RESOLUTION — tag these items [RESOLVED by exp_XXX] in self_state.json:
- Item 31: "What is the theoretical minimum ECE..." → RESOLVED by exp_774 (isotonic regression calibration)
- Item 41: "Can contradiction-only detection..." → RESOLVED by exp_773 (94.4% accuracy, 1.58x speedup)
```

The synthesis worker reads this section and prepends `[RESOLVED by exp_XXX]` to each listed item's text in `self_state.json`'s `curiosity_queue`.

## Prevention

During queue triage (step 4 of Director workflow), after classifying items as DONE/answered, always:
1. Record which items should be tagged RESOLVED
2. Include the list in the synthesis task body under a "QUEUE RESOLUTION" header
3. This ensures the synthesis worker tags them during its self_state.json update pass

## Relationship to Existing Pitfalls

- Complements `director-queue-curiosity-pitfall.md` (Director adding curiosities via kanban_comment)
- Both follow the same principle: Director cannot write to self_state.json, must communicate via kanban task body or kanban_comment
- The curiosity pitfall covers ADDING items; this covers TAGGING items as resolved
