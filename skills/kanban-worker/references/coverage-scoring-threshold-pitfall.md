# Coverage Scoring Threshold Pitfall (Cycle #248)

## Problem

The quantitative coverage scoring (Cycle #177) requires 3+ keyword matches to consider a queue item "covered" by a running task. This misses semantic matches where titles use different wording for the same concept.

## Example

Queue item: "delta-only detection ceiling — what additional signal sources could break the 5% FPR barrier?"
Running task: "Breaking the 5% FPR barrier in delta-only detection"

Shared keywords above threshold: only 1 ("barrier")
Actual coverage: FULL — same investigation

## Impact

In Cycle #248, 3 items were incorrectly classified as "OPEN" when they were semantically covered by running tasks. This led to unnecessary task creation for already-investigated topics.

## Fix Options

1. **Lower threshold to 2 keyword matches** — simple, catches most paraphrased titles
2. **Add semantic similarity scoring** — TF-IDF cosine similarity between item text and running titles
3. **Hybrid approach** — keyword match at threshold 2 OR semantic similarity > 0.3

## Detection

After running coverage scoring, manually verify items classified as "OPEN" with scores near the threshold boundary (0.10-0.20). These are most likely to be false negatives.

## Related

- Quantitative coverage scoring (Cycle #177) in kanban-worker SKILL.md
- NO_SOURCE overlap detection (Cycle #192) in kanban-worker SKILL.md
