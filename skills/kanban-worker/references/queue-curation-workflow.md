# Queue Curation Workflow

## When to Curate

Queue > 50 items → create a curation task. The Director creates it as maintenance (not investigation), even during saturation.

## Scorer Output Interpretation

The `curiosity_scorer.py --json` output classifies each queue item into threads:

| Thread | Meaning | Action |
|--------|---------|--------|
| `already_answered` | Source experiment completed and answered the question | Tag RESOLVED, remove |
| `resolved` | Already tagged [RESOLVED] by prior synthesis | Remove |
| `other` | Active research questions not in a named thread | Keep, prioritize by score |
| `injection` | Injection/robustness research thread | Keep, apply 35% diversity cap |
| `tfidf` | TF-IDF/statistical features thread | Keep, apply 35% diversity cap |

**Key insight (Cycle #576):** A queue with 67 items may only have 14 genuinely active items. The scorer reveals that 43+ items are `already_answered` — they were never cleaned up. Always run the scorer before creating curation tasks to understand the actual workload.

## Curation Task Assignment

**CRITICAL: Curation workers CANNOT write to self_state.json.** The `approvals.mode: manual` + `approvals.cron_mode: deny` configuration blocks autonomous workers from terminal writes to protected files. Workers that try will get `pending_approval` and time out silently.

**Two valid approaches:**

### Option A: Assign to prometheus-synthesis (preferred)
The synthesis worker CAN write to self_state.json (it's the single-writer). Assign the curation task to `prometheus-synthesis`. The task body should list what to clean up.

### Option B: Assign to a free worker with kanban_comment reporting
Assign to a free worker. The worker reads the queue, identifies items to remove, and reports via `kanban_comment` on a synthesis task. The synthesis worker applies the changes.

**Do NOT** assign curation to a regular worker and expect it to write self_state.json — it will silently fail.

## Curation Task Body Template

```
QUEUE CURATION TASK:

Queue has N items — above the 50-item threshold.

Steps:
1. Read self_state.json curiosity_queue
2. Run curiosity_scorer.py --json to classify threads
3. Tag resolved items: [RESOLVED by exp_NNN] for items whose source experiment answered the question
4. Remove items tagged [RESOLVED] or [already_answered] by scorer
5. De-duplicate items asking the same question (3-5x duplication observed at scale)
6. Remove items older than 100 cycles with no progress
7. Keep queue at 30-50 items
8. Report findings via kanban_comment — the synthesis worker will handle the write

CRITICAL: Curation workers CANNOT write to self_state.json directly. Report what should be changed via kanban_comment on a synthesis task.
```

## Metrics Counter Drift During Curation

When curating, also check for metrics drift:
- Compare `len(experiments.completed)` against `metrics.experiments_completed`
- If they differ, note the drift in the curation report
- The synthesis worker should reconcile all counters to `len(completed)` in a single atomic write

## Anti-Pattern: Curation as Investigation

Curation is MAINTENANCE, not investigation. It should run even during saturation (≥7 running tasks). Do not skip curation because the board is full — a bloated queue wastes assessment time on every Director pass.

## Anti-Pattern: Creating Curation Tasks for Regular Workers

Regular workers (prometheus-worker-N) hit the approval system when writing to self_state.json. The task appears to complete but the file write silently fails. Detection: self_state.json mtime stays old while the curation task shows as done. Always assign curation to prometheus-synthesis or use the kanban_comment pattern.
