# Batch Creation Script Coverage Check Pitfall

## Problem (Cycle #245)

`batch_create_tasks.py` uses Jaccard overlap thresholds to determine if a queue item is "covered" by running tasks or completed experiments. Thresholds were tightened June 3 2026: running task overlap 0.6→0.45, hypothesis overlap 0.6→0.45, result overlap 0.75→0.55. Data source upgraded from self_state.json to prometheus.db for richer dedup. See batch_create_tasks.py header for current values.

### Example from Cycle #245

Queue item: "Meta-classifier AUC-ROC=0.9939 for rule-based failure prediction — can dynamic routing with learned classifiers break rule-based plateau?"

Running task: "exp_AUTO: [Script-aware gating 40% FPR reduction but CJK blocked — does multilingual embedding retraining"

Jaccard overlap: ~0.3 (share "rule", "based", "failure" but item has "meta-classifier", "AUC-ROC", "dynamic routing" not in running title)

Result: Script classified item as COVERED (0.3 < 0.5 threshold) → 0 tasks created despite 12 free workers.

## Root Cause

The coverage check on line 193 of `batch_create_tasks.py`:
```python
overlap = len(words1 & words2) / max(len(words1 | words2), 1)
if overlap > 0.5:
    is_covered = True
```

This compares FULL queue item text against FULL running title text. Queue items are typically 80-120 words; running titles are 15-25 words. The Jaccard denominator (union) is dominated by the queue item's unique words, making the threshold effectively unreachable for items longer than titles.

## Fallback Pattern

When `batch_create_tasks.py --dry-run` shows `uncovered: 0` but `free_workers: N` where N > 3:

1. Manually classify queue items using keyword overlap with a lower threshold:
   - LOW coverage (<0.15): genuinely uncovered → create task
   - MED coverage (0.15-0.3): partially covered → create if high priority
   - HIGH coverage (>0.3): covered → skip

2. Use the Director flowchart step 4 classification (RESOLVED/RUNNING/DONE/NO_SOURCE) to filter first, then apply keyword scoring to remaining items.

3. Create tasks individually via `hermes kanban create` (not via the batch script) when the script's coverage check is blocking valid items.

## Fix for batch_create_tasks.py

The Jaccard threshold should be lowered to 0.2, OR the comparison should use title-to-title instead of title-to-full-text. The current 0.5 threshold was calibrated for similar-length strings, but queue items and running titles have very different lengths.

## False Negative: Same-Topic Duplicates Not Caught (added 2026-06-02)

The coverage check can also MISS duplicates — marking an item as uncovered when it actually duplicates a running experiment. This happens when:

1. **Timing desync**: The batch creator reads `kanban list --status running` but the task was dispatched in the same Director pass (or just before), so it doesn't appear in the list yet.
2. **Near-identical titles with different exp IDs**: Queue item text matches a running task title almost exactly, but the script's keyword extraction or Jaccard comparison misses it due to noise words or formatting differences.
3. **Topic overlap via queue item text vs running task title**: Queue item says "dispatch_overhead is a DOMAIN ANCHOR" while running task says "exp_2102: dispatch_overhead is a DOMAIN ANCHOR" — same topic, but the script's keyword matching doesn't catch it because the queue item text includes synthesis prefixes and the running title includes an exp ID.

### Example — Batch Creator Creates Duplicates (2026-06-02, Director Pass 589)

Running: exp_2102 "dispatch_overhead is a DOMAIN ANCHOR" + exp_2121 (same topic)
Batch creator created: exp_2131 "dispatch_overhead is a DOMAIN ANCHOR" (same topic, different ID)

Also: Running: exp_2114, exp_2123, exp_2124 all about "TF-IDF+RF compression"
Batch creator created: exp_2132 "TF-IDF+RF compression" (same topic, different ID)

The batch creator reported `uncovered: 3` and created 2 tasks that duplicated running experiments. Post-creation topic duplicate detection caught both.

**Why the coverage check missed these:** The script's Jaccard comparison uses queue item text (which includes synthesis prefixes like "[NEW from synthesis v587]") against running task titles (which include exp IDs). The noise words dilute the overlap score below the threshold.

### Mitigation (mandatory after batch creation)

Always run topic duplicate detection AFTER batch_create_tasks.py and BEFORE dispatch:

```python
import json, re
from collections import defaultdict

running = json.load(open('/tmp/kanban_running.json'))
new_tasks = json.load(open('/tmp/kanban_ready.json'))  # or wherever new tasks appear

# Normalize topic text (strip exp IDs and prefixes)
def normalize_topic(title):
    return re.sub(r'exp_\d+\w*:\s*', '', title).lower().strip()[:60]

topics = defaultdict(list)
for t in running + new_tasks:
    topic = normalize_topic(t.get('title', ''))
    topics[topic].append(t['id'])

for topic, ids in topics.items():
    if len(ids) > 1:
        print(f"DUPLICATE: {topic} → {ids}")
        # Reclaim the newer task (higher exp number), block to prevent re-dispatch
```

When duplicates found: `hermes kanban reclaim <newer_id>` then `hermes kanban block <newer_id> "duplicate of <older_id>"`.

## Domain-Specific Coverage Inflation (added Cycle #600)

When all queue items and running tasks share domain vocabulary (e.g., injection detection research with terms like "injection", "detection", "TF-IDF", "LR", "F1", "AUC"), the keyword-overlap coverage scoring inflates even for genuinely distinct experiments. In Cycle #600, 25 queue items had coverage scores 0.31-0.71 against 8 running tasks — all investigating different hypotheses but sharing ~40% of keywords. The batch creator reported "Uncovered: 0" despite 16 free workers.

**Detection:** If `batch_create_tasks.py --dry-run` reports 0 tasks when 10+ workers are free and queue has 20+ items, manually check coverage scores. Items with coverage 0.3-0.5 in domain-saturated research may still be genuinely uncovered.

**Fix:** Lower `--min-score` to 30 to surface more items, then manually create tasks for LOW-coverage (<0.15) items. The batch creator's coverage check is advisory for domain-saturated research areas — manual override is expected.

## --min-score Bypasses Coverage Check (added Cycle #246)

When lowering `--min-score` to bypass aggressive coverage filtering (e.g., `--min-score 30`), the batch creator selects items that ARE covered by running tasks. The coverage check is effectively disabled at lower thresholds because the score filter replaces it as the primary gate.

### Example (Cycle #246)

Board: 13 running tasks, 7 free workers.
Queue: 36 items, 24 active.

Batch creator at default (min-score 60):
```
Queue: 24 active items, 2 score >= 60
Uncovered by running tasks: 0
Selected: 0 tasks
```

Batch creator at `--min-score 30`:
```
Queue: 24 active items, 24 score >= 30
Uncovered by running tasks: 12
Selected: 7 tasks
```

Of the 7 selected tasks, 2 were already covered by running experiments:
- "EU AI Act deference 20% drop" → already running as exp_2202
- "Direct LR 36x faster" → already running as exp_2256

The coverage check reported 12 uncovered, but the selected items included duplicates because the lower score threshold let through items whose coverage score was above the keyword overlap threshold but below the score threshold.

### Example — Calibration Item with 3 Running Duplicates (2026-06-03, Director Pass 247)

Queue item: "Isotonic calibration production-scale needs retry — what chunking strategy avoids 127min timeout?"
Score: 88 (HIGH). Batch creator reported: "Uncovered by running tasks: 13, Selected: 5 tasks"
But running tasks already included exp_2387, exp_2391, exp_2405 — ALL about isotonic calibration.

**Why missed:** The batch creator's full-text Jaccard compares the queue item's ~20 words against running titles' ~15 words. The queue item includes "chunking strategy" and "timeout" which aren't in running titles, diluting the overlap below the threshold. The batch creator sees 3 running tasks with different specific angles (chunking, production-scale, retry) as covering different sub-topics.

**Detection after batch dry-run:** Strip exp IDs and prefixes from both sides, then check keyword overlap on core terms only:
```python
# If 3+ running titles share the same 2-3 core nouns as the selected item, it's a duplicate
running_cal = [t for t in running if 'calibrat' in t.get('title','').lower()]
if len(running_cal) >= 2:
    print(f"DUPLICATE: {item['title'][:60]} ({len(running_cal)} running cal tasks)")
```

### Detection

After `batch_create_tasks.py --min-score 30 --dry-run`, manually check selected items against running task titles:
```python
import json, re
selected = [...]  # items from dry-run output
running_titles = ' '.join(t.get('title', '').lower() for t in running)
for item in selected:
    # Strip exp IDs and prefixes for comparison
    clean = re.sub(r'exp_\d+\w*:\s*', '', item['title']).lower()
    words = set(clean.split())
    overlap = sum(1 for w in words if len(w) > 3 and w in running_titles)
    if overlap > 3:
        print(f"LIKELY COVERED: {item['title'][:60]} (overlap={overlap})")
```

### Fix

After batch creation, always run topic duplicate detection (see existing "False Negative: Same-Topic Duplicates Not Caught" section above) to catch items the coverage check missed. Reclaim and block any duplicates found.

## Related

- Quantitative coverage scoring (Cycle #177) in kanban-worker SKILL.md
- NO_SOURCE overlap detection (Cycle #192) in kanban-worker SKILL.md
- Reverse sync false negative (Cycle #600) in `references/reverse-sync-false-negative.md`
