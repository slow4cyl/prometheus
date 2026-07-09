# batch_create_tasks.py Threshold Pitfall

## Problem

The `batch_create_tasks.py` script's default `--min-score 60` threshold can be too aggressive when queue items have scores in the 40-58 range. This causes the Director to report "0 tasks to create" even when there are 20+ active queue items that need investigation.

## Observed in Cycle #245

- Queue: 22 active items
- Default threshold (--min-score 60): Only 2 items scored ≥60, both covered by running tasks → 0 tasks created
- Reduced threshold (--min-score 40): 21 items scored ≥40, 20 uncovered → 14 tasks created

## Detection

When `batch_create_tasks.py` reports:
- "Uncovered: 0" but queue has 20+ active items
- "Selected: 0 tasks" with many free workers

The threshold is likely too high.

## Fix

Run with adjusted threshold:
```bash
# Check score distribution first
python3 ~/.hermes/scripts/batch_create_tasks.py --count 18 --min-score 30 --json > /tmp/batch_scores.json

# Then create with appropriate threshold
python3 ~/.hermes/scripts/batch_create_tasks.py --count 18 --min-score 40
```

## Rule

- If median queue score <50: use `--min-score 40`
- If median queue score >60: default threshold is fine
- Always check score distribution before deciding threshold

## Why This Matters

Idle workers = wasted capacity. When the Director reports 0 tasks but has 18 free workers, it's a missed opportunity to make progress on the research queue.

## Related

- See `director-pass-checklist-cycle272.md` for the full Director pass workflow
- See `curiosity-scorer-json-format.md` for the scorer output format
