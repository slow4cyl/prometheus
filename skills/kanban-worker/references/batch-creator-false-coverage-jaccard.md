# Batch Creator False Coverage — Jaccard Overlap Pitfall

## Problem
The `batch_create_tasks.py` script's coverage check uses Jaccard word overlap (> 0.5) against running task titles. When the queue is terminology-dense (all items share TF-IDF/injection/domain vocabulary), this flags every item as "covered" — producing 0 tasks even with 9+ free workers.

## Detection
```
python3 ~/.hermes/scripts/batch_create_tasks.py --count 9 --dry-run
# Output: "Uncovered by running tasks: 0" with 10+ NO_SOURCE items in queue
```

## Fix
Lower `--min-score` from default 60 to 30:
```
python3 ~/.hermes/scripts/batch_create_tasks.py --count 9 --min-score 30
```

The diversity cap (35% per thread) still prevents thread monopolization.

## Rule of Thumb
If dry-run shows 0 uncovered but you have ≥3 free workers and ≥5 NO_SOURCE queue items, always retry with `--min-score 30`.

## Why This Happens
The Jaccard check computes `len(words1 & words2) / len(words1 | words2)` where words are 4+ character tokens. Queue items like "TF-IDF+LR vocabulary overlap threshold" and "TF-IDF+LR standalone preferred" share most tokens → overlap > 0.5 → falsely flagged as covered. The check can't distinguish "same topic, different question" from "duplicate work."

## Observed In
- Session where 12 running tasks included 6 TF-IDF/domain items, causing all 16 NO_SOURCE items to appear covered
- batch_create_tasks.py with default --min-score 60 produced 0 tasks; with --min-score 30 produced 9 tasks
