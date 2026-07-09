# Synthesis-Covered NO_SOURCE Items

## Problem

When dispatching a synthesis task for recently completed experiments, the Director may also identify NO_SOURCE queue items that need new tasks. But some NO_SOURCE items are answered by the experiments being synthesized — creating tasks for them wastes worker slots.

## Example (Cycle #216)

Queue items 29-31 were NO_SOURCE:
- Item 29: "Why does safety training degrade with model size?" (from synthesis v341)
- Item 30: "Can JS divergence predict calibration quality?" (from synthesis v341)
- Item 31: "What is the theoretical minimum ECE?" (from synthesis v341)

The synthesis task consolidated exp_744-747:
- exp_744: JS divergence calibration generalization
- exp_745: Isotonic regression minimum ECE + safety training degradation
- exp_746: JS divergence counterintuitive finding
- exp_747: Theoretical minimum ECE

These experiments directly answer items 29-31. Creating new tasks would waste workers on questions the synthesis task will resolve.

## Detection Pattern

After dispatching a synthesis task, cross-reference the experiments being synthesized against NO_SOURCE queue items:

```python
import re

# Extract topics from synthesis task body
synth_experiments = ['exp_744', 'exp_745', 'exp_746', 'exp_747']

# For each NO_SOURCE queue item, check if any synthesis experiment's topic covers it
for item in no_source_items:
    item_keywords = set(item.lower().split()) - stop_words
    # Check if synthesis experiments share keywords with this item
    for exp_id in synth_experiments:
        exp_summary = get_experiment_summary(exp_id)  # from self_state.json
        exp_keywords = set(exp_summary.lower().split()) - stop_words
        overlap = item_keywords.intersection(exp_keywords)
        if len(overlap) >= 3:  # significant overlap
            print(f"DEFER: {item[:60]} — covered by {exp_id}")
```

## Rule

When a synthesis task is dispatched for experiments that answer NO_SOURCE queue items:
1. Note which items will be resolved
2. Do NOT create tasks for those items
3. The synthesis worker will tag them `[RESOLVED by exp_XXX]` when it processes the experiments
4. Only create tasks for NO_SOURCE items whose questions are NOT covered by the synthesis batch

## Priority

This check should happen AFTER creating the synthesis task but BEFORE creating experiment tasks. The synthesis task creation is Step 3 in the Director flowchart; the NO_SOURCE task creation is Step 6. Insert the cross-reference between these steps.
