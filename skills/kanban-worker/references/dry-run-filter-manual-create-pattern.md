# Dry-Run → Filter → Manual Create Pattern (June 2026)

When `batch_create_tasks.py --dry-run` produces tasks that overlap with running experiment topics, the Director should NOT create all suggested tasks. Instead: filter the dry-run output, then manually create only the clean tasks.

## The Pattern

1. Run `batch_create_tasks.py --dry-run --count <N> --min-score 30`
2. Parse the dry-run output to extract selected queue items and their topics
3. Compare each topic against running task titles (Step 5b)
4. Skip any task whose topic is already covered by a running experiment
5. Manually `hermes kanban create` only the non-duplicate tasks for verified-free workers

## Example (June 3, 2026)

Dry-run suggested 6 tasks (scores 85-88, 4 threads):
- injection × 2 → workers 1, 8
- calibration × 1 → worker 13
- embedding × 1 → worker 15
- tfidf × 2 → workers 23, 24

Running tasks already included:
- exp_2845: "TF-IDF outperforms neural on vocabulary-overlap..." (same topic as tfidf #2)
- exp_2905: "Neural embeddings +24.57pp F1 but 7-31x late" (same topic as tfidf #1)

Result: Created 3 tasks manually (workers 1, 13, 15), skipped 2 tfidf duplicates. All 3 dispatched successfully.

## Why Not Just Use the Batch Creator?

The batch creator's 35% diversity cap limits per-thread creation but does NOT check running task topics (documented pitfall: "topic duplicates slip through diversity cap"). The dry-run mode is the only way to inspect what would be created before committing.

## Worker Verification

Before manually creating, verify each target worker is actually free by checking kanban task assignments (not just `ps aux`):
```python
# Cross-reference: which profiles have running tasks?
hermes kanban list --status running --json | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
busy = set(t.get('assignee','') for t in tasks if t.get('assignee'))
print('Busy profiles:', sorted(busy))
"
```

A worker can appear in `ps aux` (launcher process) but have no running kanban task — the batch creator correctly counts it as free. Trust the kanban assignment list for "is this worker busy", not `ps aux`.

## Audit Trail

Log the filtering decision to `self_audit.log`:
```
2026-06-03T14:02:50 | DIRECTOR PASS | Batch dry-run: 6 suggested, 2 filtered (tfidf dupes), created 3 manually
```
