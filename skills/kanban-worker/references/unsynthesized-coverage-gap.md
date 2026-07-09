# NO_SOURCE Coverage Must Include Unsynthesized Experiments

**Added:** Cycle #246 (2026-06-02)

## Problem

The quantitative coverage scoring checks queue items against **running task titles only**, but many NO_SOURCE items are actually covered by **UNSYNTHESIZED experiments** — completed tasks whose results haven't been synthesized into self_state.json yet.

These experiments address the same research questions as the queue items, but the coverage check misses them because it only looks at running tasks.

## Example (Cycle #246)

Queue item: "Meta-classifier AUC-ROC=0.9939 for rule-based failure prediction — can dynamic routing with real ML models achieve the expected 3-5pp F1 gain?"

- **Running tasks coverage:** LOW (0.12) — no running task about meta-classifiers
- **Including unsynthesized:** exp_1637 "Meta-classifier dynamic routing with real ML models" → coverage jumps to HIGH (0.75)

Without including unsynthesized experiments, this item would be incorrectly flagged as uncovered, leading to a wasted duplicate task.

## Fix

When computing coverage, include titles from ALL experiment sources:

```python
# Running tasks
running_text = ' '.join(t.get('title', '') for t in running).lower()

# Unsynthesized experiments (completed but not in self_state.json)
unsynth_titles = [
    'exp_1631: Embedding modality edge cases',
    'exp_1633: 2-feature F1=0.999967',
    # ... from done_exp_ids - ss_exp_ids
]
unsynth_text = ' '.join(unsynth_titles).lower()

# Completed tasks (also done, for belt-and-suspenders)
done_titles = [t.get('title', '') for t in done if not is_synth(t)]
done_text = ' '.join(done_titles).lower()

# Combined coverage corpus
all_exp_text = running_text + ' ' + unsynth_text + ' ' + done_text
```

## Detection

If the curiosity scorer reports items scoring >= 60 but the queue classification shows them as NO_SOURCE with LOW coverage against running tasks only, suspect unsynthesized coverage gap. Re-run coverage with the full experiment text corpus.

## Impact

In Cycle #246, ALL 17 active NO_SOURCE items showed HIGH coverage only after including unsynthesized experiment titles. Without this inclusion, 10+ items would have been incorrectly flagged as uncovered.
