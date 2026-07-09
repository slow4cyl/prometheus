# batch_create_tasks.py Selects Topic-Covered Items

## Problem (Director Pass #610, Cycle #245)

With 19 running tasks and 3 free workers, `batch_create_tasks.py --count 4 --min-score 30 --dry-run` selected 4 tasks that were ALL topic-duplicates of running experiments:

```
Selected: 4 tasks
  1. [ 58] Task-complexity-adaptive ensembles → worker-7
  2. [ 54] EU AI Act deference 20% drop → worker-10
  3. [ 54] Direct LR 36x faster → worker-13
  4. [ 50] Ensemble disagreement AUC → worker-18
```

Each selected item had a running experiment on the SAME research question:
- "Task-complexity-adaptive ensembles" → exp_2281 AND exp_2285 already running
- "EU AI Act deference 20% drop" → exp_2214 already running
- "Direct LR 36x faster" → exp_2256 already running
- "Ensemble disagreement AUC" → exp_2218 already running

Meanwhile, a manual keyword coverage check (stop-word filtering + title overlap) found 0 items with coverage < 0.15 — ALL queue items were covered.

## Root Cause

The batch creator's coverage logic has two layers:
1. **Source-ID check**: Filters items whose `exp_NNN` source is in a running task title
2. **Jaccard keyword overlap**: Checks keyword similarity between queue item and running titles

Layer 1 misses NO_SOURCE items (no source ID). Layer 2 uses Jaccard similarity on full text, which can produce LOW overlap scores even when the core topic matches exactly. Example:

- Queue: "Task-complexity-adaptive ensembles — is the simple/medium/hard taxonomy reliable"
- Running: "exp_2281: Task-complexity-adaptive ensembles — is the simple/medium/hard taxonomy reliable"
- Jaccard on full text: high overlap BUT the script's normalization strips experiment IDs and prefixes, reducing the effective overlap

The diversity cap also plays a role: it limits per-thread creation to `floor(0.35 × N)`, so even with many qualifying items, only a few are selected — and those few can all be from the same saturated thread.

## Detection

After `batch_create_tasks.py --dry-run`, for EACH selected candidate:
1. Extract the core research question (strip `[NEW from synthesis vXXX]` prefix and experiment ID suffix)
2. Search running task titles for the same core question
3. If found → skip this candidate, it's a duplicate

```python
import json, re

running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '').lower() for t in running]

candidates = [
    "Task-complexity-adaptive ensembles — is the simple/medium/hard taxonomy",
    "EU AI Act deference 20% drop - is this a general pattern",
    "Direct LR 36x faster but training 4.4x longer",
    "Ensemble disagreement AUC 0.84-0.97 — what's the theoretical ceiling",
]

for c in candidates:
    # Extract core phrase after exp_NNN: prefix
    core = re.sub(r'exp_\d+:\s*', '', c).lower()
    core_words = [w for w in core.split() if len(w) > 3][:8]  # first 8 significant words
    overlaps = sum(1 for w in core_words if any(w in t for t in running_titles))
    coverage = overlaps / max(len(core_words), 1)
    status = "COVERED" if coverage > 0.3 else "OPEN"
    print(f"  {status} (cov={coverage:.2f}): {c[:70]}")
```

## Mitigation

**Always cross-reference batch creator output against running tasks before creating.** The script's "Uncovered by running tasks" count checks source-ID coverage, not topic coverage. Items with NO_SOURCE (no experiment ID reference) are always "uncovered" by ID even when their topic is actively being investigated.

When the batch creator selects covered items:
1. Note which candidates are duplicates
2. Replace them with genuinely distinct items from the uncovered list
3. Or: create fewer tasks than free workers (idle workers > wasted duplicate work)

**Rule: If ALL selected candidates are topic-covered, create ZERO tasks and log as convergence — the queue is fully covered by running experiments.**
