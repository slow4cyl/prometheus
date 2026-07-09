# Intra-Batch and Intra-Queue Deduplication

## Problem

The Director's synthesis output generates duplicate curiosity queue items across cycles.
When `batch_create_tasks.py` scores the queue, two identical items can both score high
enough to pass the threshold. The old dedup only checked against completed experiments
(retrospective), not against other items being created in the same batch (prospective).

### Observed Impact (June 2026)
- exp_2115 and exp_2127: identical hypothesis ("Norm+TF-IDF AUC=0.9888 on real-world"),
  created 17 microseconds apart in the same batch
- exp_2109 and exp_2141: identical hypothesis ("Real-world attack taxonomy — roleplay dominates")
- Both pairs had Jaccard overlap = 1.0 (perfect match) but passed dedup because:
  1. The hypothesis overlap threshold (0.4) only checked against ALREADY completed experiments
  2. Within a single batch, items are scored sequentially — the first item hasn't been
     "completed" yet when the second item is checked

## Three-Layer Dedup Architecture

### Layer 1: Queue-Level (curiosity_scorer.py)
**Function:** `is_already_in_queue(item_text, existing_queue)`
**Threshold:** >0.7 word overlap (4+ char words)
**When:** At scoring time — `score_all()` tracks `seen_texts` and marks duplicates
**Effect:** Duplicate queue items get score=0, thread="duplicate", resolved=True

```python
# In score_all():
seen_texts = []
for item in queue:
    text = str(item)
    if is_already_in_queue(text, seen_texts):
        # Score as duplicate, skip
        continue
    seen_texts.append(text)
    result = score_item(text, exp_index, recent_threads)
```

### Layer 2: Batch-Level (batch_create_tasks.py)
**When:** During task selection — checks each candidate against already-selected items
**Threshold:** >0.7 word overlap
**Effect:** Prevents creating two tasks from two different but identical queue items

```python
# In the selection loop:
for item in uncovered:
    item_words = set(re.findall(r"\w{4,}", item["text"].lower()))
    is_dup = False
    for sel in selected:
        sel_words = set(re.findall(r"\w{4,}", sel["text"].lower()))
        overlap = len(item_words & sel_words) / max(len(item_words | sel_words), 1)
        if overlap > 0.7:
            is_dup = True
            break
    if is_dup:
        continue
    selected.append(item)
```

### Layer 3: Completed Experiment Check (batch_create_tasks.py)
**When:** Before scoring — checks queue items against completed experiments
**Thresholds (tightened June 3 2026):** Hypothesis overlap >0.45, Result overlap >0.55, Running task overlap >0.45
**Effect:** Prevents re-investigating already-answered questions
**Data source:** prometheus.db (primary, full hypotheses+results), self_state.json (fallback)
**Note:** Previous thresholds (0.6/0.75/0.6) allowed 29% duplicate rate. Tightened to catch more semantic matches.

## Tokenization

All layers use the same tokenization: `re.findall(r'\w{4,}', text.lower())`
- Minimum 4 characters (filters "the", "what", "does", etc.)
- Lowercase for case-insensitive matching
- Word boundaries only (no subword splitting)

## Threshold Tuning History

| Date | Change | Reason |
|------|--------|--------|
| Cycle #583 | Hypothesis threshold 0.4→0.6 | 41/52 items filtered as "already answered" |
| Cycle #583 | Result threshold 0.45→0.65 | Same — too aggressive |
| Cycle #583 | Running task threshold 0.5→0.6 | Same |
| Cycle #601 | Hypothesis threshold 0.6→0.6 | Already at 0.6 from Cycle #583 |
| Cycle #601 | Result threshold 0.65→0.75 | Follow-up experiments filtered as "covered" |
| June 2026 | Added intra-batch dedup at 0.7 | Duplicate experiments in same batch |
| June 2026 | Added intra-queue dedup at 0.7 | Duplicate queue items across cycles |

## Edge Cases

1. **Queue items are strings, not dicts** — `self_state.json` stores queue as `["item text", ...]`
2. **Hypotheses are truncated** — `completed_hyps[eid] = exp["hypothesis"].lower()` (no truncation)
3. **Results are truncated to 300 chars** — `completed_results[eid] = exp["result"].lower()[:300]`
4. **The scorer runs before batch_create** — so Layer 1 dedup happens first, reducing the queue
   before Layer 2 even sees it
5. **Director can override** — the Director is an LLM; it can create tasks manually that bypass
   batch_create_tasks.py entirely. The intra-batch dedup only applies to script-created tasks.
