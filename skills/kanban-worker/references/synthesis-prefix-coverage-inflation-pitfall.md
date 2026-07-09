# Synthesis Prefix Inflating Queue Coverage Scores

**Added: Cycle #244 (Director pass)**

## Problem

When doing manual coverage scoring (queue triage), the `[NEW from synthesis vXXX]` prefix in queue items contains the word "synthesis" which matches running synthesis task titles (e.g., "SYNTHESIS: consolidate 13 unsynthesized experiments"). This inflates coverage scores, making genuinely UNCOVERED items appear COVERED.

## Root Cause

The coverage scoring code extracts keywords from queue item text:
```python
words = set(re.findall(r'[a-z]+', text.lower()))
keywords = {w for w in words - stop if len(w) > 2}
```

The stop words don't include "synthesis". So every queue item with a `[NEW from synthesis vXXX]` prefix contributes "synthesis" as a keyword. When a synthesis task is running, this word matches, inflating coverage by 1 keyword per item.

With 12-15 running tasks (including a synthesis task), this can push coverage scores from LOW to HIGH for items that are genuinely uncovered.

## Example (Cycle #244)

Queue item: `[NEW from synthesis v633] Power normalization (0.3-0.7) fixes attention transfer asymmetry — does this generalize to other architectures?`

Running tasks include: `SYNTHESIS: consolidate 13 unsynthesized experiments`

Coverage keywords extracted: `{'power', 'normalization', 'fixes', 'attention', 'transfer', 'asymmetry', 'generalize', 'architectures', 'synthesis'}`

Matched against running titles: `synthesis` → 1 match out of 9 keywords → coverage = 0.11 (LOW)

But with more running tasks sharing common words, coverage can inflate to MED or HIGH.

In the actual pass, ALL 4 items selected by `batch_create_tasks.py` were COVERED by running experiments, but the manual coverage check using the reference code also showed inflated coverage due to the synthesis prefix.

## Fix

Strip the `[NEW from synthesis vXXX]` prefix (and any similar bracketed prefixes) before extracting keywords:

```python
import re

def clean_queue_text(text):
    """Strip synthesis prefixes and experiment ID prefixes for coverage scoring."""
    # Remove [NEW from synthesis vXXX] prefix
    text = re.sub(r'\[NEW from synthesis v\d+\]\s*', '', text)
    # Remove [RESOLVED by exp_XXX] prefix
    text = re.sub(r'\[RESOLVED.*?\]\s*', '', text)
    # Remove exp_NNN: prefix
    text = re.sub(r'^exp_\d+\w*:\s*', '', text)
    return text

# Use in coverage scoring:
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    text = clean_queue_text(text)  # Strip prefixes before keyword extraction
    words = set(re.findall(r'[a-z]+', text.lower()))
    keywords = {w for w in words - stop if len(w) > 2}
    # ... rest of coverage scoring
```

## Also: Add "synthesis" to stop words

Even with prefix stripping, the word "synthesis" can appear in legitimate queue item text. Add it to the stop words set:

```python
stop = {'the', 'a', 'an', ... 'new', 'we', 'synthesis'}
```

## Impact

Without this fix, the Director may:
1. Skip creating tasks for genuinely uncovered items (false HIGH coverage)
2. Create tasks for items already covered by running experiments (batch creator selects covered items)
3. Both waste worker slots and leave queue items unresolved

## Detection

After coverage scoring, manually check items with MED/HIGH coverage (0.15-0.3+) that have `[NEW from synthesis vXXX]` prefix. If the only matched keyword is "synthesis" or other prefix-related words, the item is likely UNCOVERED despite the score.

## Related

- `coverage-scoring-conceptual-overlap-pitfall.md` — broader issue of different terminology for same concept
- `batch-creator-coverage-false-negative.md` — batch creator's coverage check has same issue
- `batch-creator-selects-covered-items.md` — batch creator selecting covered items
- `queue-triage-with-coverage-scoring.md` — the reference code that needs this fix
