# Batch Creator Worker Count Desync — Duplicate Creation Pattern

## Problem

The `batch_create_tasks.py` script determines free worker count from `kanban list --status running`, which can include status-desync'd done tasks. This inflates the busy count, causing the script to under-count free workers and create fewer tasks than needed.

When the Director then manually creates tasks for the remaining "free" workers, the manually-created tasks can overlap topically with the batch-created tasks, producing duplicate experiments.

## Observed in Cycle #259

- **Batch creator reported:** "Workers: 4 free, 18 busy"
- **Actual (ps aux):** 8 alive processes, 14 free workers
- **Batch created:** 4 tasks (diversity cap limited to 4 threads)
- **Manual creation:** 10 tasks for remaining workers
- **Result:** 4 of the 10 manual tasks overlapped with batch-created topics → 10 duplicate pairs

### Duplicate pairs observed:
| Batch-created | Manual-created | Topic |
|---|---|---|
| exp_2622 (t_26652bcb) | exp_2633 (t_9991b921) | Dynamic ensemble sizing |
| exp_2623 (t_425c6e3d) | exp_2631 (t_e62cca2d) | Standalone TF-IDF+LR at 100+ domains |
| exp_2624 (t_f69c02a1) | exp_2632 (t_c966bd01) | C=50.0 optimal across ALL injection types |
| exp_2627 (t_f3c92116) | exp_2637 (t_4c1b5c86) | Char TF-IDF+GB obfuscation types |
| exp_2628 (t_00f52ef0) | exp_2639 (t_7e98b2f6) | No universal minimal feature set for RF |

Plus 5 more pairs from other overlapping topics.

## Root Cause

1. **Status desync:** `kanban list --status running` returns tasks that are actually done (status desync documented in kanban-worker skill). The batch creator counts these as "busy".
2. **Batch creator uses kanban list, not ps aux:** The script reads kanban list output to determine worker availability, not actual process liveness.
3. **No cross-check between batch and manual creation:** The Director manually creates tasks without checking what the batch creator already selected.

## Fix

### Before batch creation:
```bash
# Verify free workers via process liveness (ground truth)
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
```
Subtract from full set (1-22). If batch creator's "N free" differs from ps-based count by >3, the batch creator is reading stale data.

### After batch creation, before manual creation:
```python
# Extract topics from batch-created tasks
batch_topics = [t.get('title', '').lower() for t in batch_created_tasks]

# When creating manual tasks, skip items whose topic overlaps with batch topics
for item in candidates:
    if any(overlap(item.text, bt) for bt in batch_topics):
        continue  # Skip — batch already covers this
```

### Alternative: Skip batch creator entirely
When free worker count is known accurately (via ps aux), manually create ALL tasks without using batch_create_tasks.py. This avoids the desync issue entirely but loses the batch creator's scoring and diversity cap.

## Detection

After creating tasks (batch + manual), run:
```bash
ps aux | grep 'kanban task' | grep -v grep | grep -oE 'kanban task (t_[a-f0-9]+)' | sort -u | wc -l
```
If count > expected workers, duplicates exist. Cross-reference task titles to identify which to reclaim.

## Recovery

1. Identify duplicate pairs by topic overlap
2. Keep the newer task (higher exp number), reclaim the older: `hermes kanban reclaim <older_task_id>`
3. Kill lingering processes for reclaimed tasks
4. Verify no workers have 2 tasks: `ps aux | grep prometheus-worker | grep -oE 'prometheus-worker-[0-9]+|kanban task (t_[a-f0-9]+)'`
