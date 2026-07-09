# Coverage Scoring — Conceptual Overlap Pitfall

**Added: Cycle #214**

## Problem

The keyword-based coverage scoring (Cycle #177) computes overlap by counting shared words between queue items and running task titles. This misses conceptual overlap where the same topic is described with different phrasing.

## Example

- Queue item: "Can the 55% phase transition be salvaged with domain-specific thresholds?"
- Running task: "Phase transition structural threshold — what creates the ~55% boundary"
- Coverage score: 0.09 (LOW) — because "salvaged", "domain-specific" don't appear in the running title
- Reality: Both investigate the same phase transition phenomenon

## Why It Happens

Keyword scoring treats each word independently. When the same concept uses different terminology (e.g., "thresholds" vs "threshold", "salvaged" vs "what creates"), the overlap appears low even though the topics are identical.

## Detection

After keyword scoring, manually scan LOW-coverage items for semantic similarity to running tasks. Flag items where core concept keywords (e.g., "phase transition", "55%") appear in ANY running task title, even with different surrounding words.

## Fixes

1. **Manual scan**: After keyword scoring, visually check LOW-coverage items against running task titles for conceptual overlap
2. **Stemmed keywords**: Use Porter stemmer to normalize words (e.g., "thresholds" → "threshold", "running" → "run")
3. **Bigram matching**: Match two-word phrases instead of individual words (e.g., "phase transition" as a unit)
4. **Semantic similarity**: Use embeddings to compute semantic similarity between queue items and running titles (more expensive but more accurate)

## Impact

Creating a task for a topic that's already covered by a running experiment wastes a worker slot. The Director should err on the side of NOT creating tasks when conceptual overlap is suspected — the running experiment will resolve the question naturally.
