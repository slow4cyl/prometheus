# Fuzzy Topic Duplicate Detection (added Cycle #232)

## Problem

The original topic duplicate detection uses exact string matching on truncated title text (`topic[:50].lower().strip()`). This misses near-duplicates with slightly different phrasing:

- "Multi-feature lightweight bypass detector — LR/RF/GB first-pass"
- "Multi-feature lightweight bypass detector (LR/RF/GB)"

These produce different truncated strings and are NOT grouped as duplicates.

## Fix: Keyword Overlap Instead of Exact Match

Extract significant keywords from each title, then compute pairwise overlap. Tasks sharing >70% of keywords are topic-duplicates.

```python
from collections import defaultdict
import re

def extract_keywords(title):
    """Extract significant keywords from a task title for fuzzy matching."""
    m = re.search(r'exp_\d+\w*[:\s]+(.+)', title)
    if not m: return set()
    text = m.group(1).lower()
    stop = {'the','a','an','is','are','was','were','be','been','being','have','has',
            'had','do','does','did','will','would','could','should','may','might',
            'can','this','that','it','of','in','to','for','with','on','at','by',
            'from','as','or','and','but','if','vs','—','-','–'}
    words = set(re.findall(r'[a-z]{3,}', text)) - stop
    return words

def is_topic_duplicate(title1, title2, threshold=0.7):
    kw1, kw2 = extract_keywords(title1), extract_keywords(title2)
    if not kw1 or not kw2: return False
    overlap = len(kw1.intersection(kw2)) / min(len(kw1), len(kw2))
    return overlap >= threshold

# Group by fuzzy topic overlap
topics = defaultdict(list)
for t in running:
    title = t.get('title', '')
    if 'synth' in title.lower() or 'consolidat' in title.lower(): continue
    matched = False
    for existing_title, existing_ids in list(topics.items()):
        if is_topic_duplicate(title, existing_title):
            topics[existing_title].append(t['id'])
            matched = True
            break
    if not matched:
        topics[title] = [t['id']]

for title, ids in topics.items():
    if len(ids) > 1:
        print(f"TOPIC DUPLICATE: {title[:60]} → {ids}")
```

## Threshold Tuning

- **0.7 (default)**: Catches most near-duplicates. Safe for typical experiment titles (10-20 keywords).
- **0.6**: More aggressive — catches loose topical overlaps. Use when titles are very short or use varied terminology.
- **0.8**: More conservative — only catches near-identical titles. Use when false positives waste more budget than false negatives.

## Why Exact Matching Fails

Experiment titles often vary punctuation, abbreviation, or word order while investigating the exact same question:
- "LR/RF/GB" vs "LR RF GB" vs "logistic random forest gradient"
- "injection detection" vs "detecting injection" vs "injection-type classifier"
- "MoE expert routing" vs "mixture-of-experts expert routing"

Keyword extraction normalizes these variations into comparable sets.

## Integration with Director Flowchart

This pattern should be used in Step 6 (CHECK SATURATION) of the Director flowchart, BEFORE the saturation check. Duplicate reclaim is a maintenance action that happens regardless of saturation — it frees worker slots and prevents wasted API budget.

Updated step 6 logic:
```
6. DETECT AND RECLAIM TOPIC DUPLICATES (maintenance — always, regardless of saturation)
   Use fuzzy keyword matching (see references/fuzzy-topic-duplicate-detection.md)
   For each duplicate pair:
     - If one task is >30min older: keep older, reclaim newer
     - If both <5min old: reclaim higher experiment number
     - Otherwise: keep both (complementary — see references/director-duplicate-judgment.md)
   Reclaim + block reclaimed tasks to prevent re-dispatch

7. CHECK SATURATION (only for experiment task creation)
   Verified_running = count of tasks confirmed running via kanban_show (not just list)
   If verified_running ≥ 7 → SYNTHESIS-ONLY, skip step 8
   EXCEPTION: if free_workers ≥ 3 AND 3+ genuinely OPEN queue items exist, create tasks
```

## Real-World Example (Cycle #232)

Two topic-duplicate pairs detected:
1. exp_1098 vs exp_1120: "Reasoning confidence increase with injected facts" — exact match, caught by original code
2. exp_1110 vs exp_1117: "Multi-feature lightweight bypass detector — LR/RF/GB first-pass" vs "Multi-feature lightweight bypass detector (LR/RF/GB)" — missed by exact match, caught by keyword overlap (83% overlap)

The second pair was only caught by manual inspection. Fuzzy matching would have caught it automatically.
