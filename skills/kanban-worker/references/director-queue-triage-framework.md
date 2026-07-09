# Director Queue Triage Decision Framework

Step-by-step decision process for classifying and prioritizing queue items during a Director pass.

## Classification Algorithm

```python
import json, re, os

def classify_queue_items(queue, running_exp_ids, done_exp_ids):
    """
    Classify each queue item into categories.
    
    Returns list of (index, category, text) tuples.
    """
    results = []
    
    for i, item in enumerate(queue):
        text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
        
        # Skip already resolved
        if 'RESOLVED' in text:
            results.append((i, 'RESOLVED', text))
            continue
        
        # Extract source experiment IDs
        source_ids = re.findall(r'exp_(\d+\w*)', text)
        
        if not source_ids:
            category = 'NO_SOURCE'  # Pure research, always valuable
        elif any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
            category = 'RUNNING'    # Source experiment still running
        elif any(f'exp_{sid}' in done_exp_ids for sid in source_ids):
            category = 'DONE'       # Source completed, question open
        else:
            category = 'UNKNOWN'    # Source not found
        
        results.append((i, category, text))
    
    return results
```

## Priority Order for Task Creation

0. **Deduplicate first** — detect and collapse identical queue items (see `references/director-queue-deduplication.md`). Deduplication reduces the effective queue size and prevents creating tasks for duplicate items.

1. **NO_SOURCE items** (highest priority) — Pure research questions without experiment references. Always valuable because they're genuinely independent.

2. **DONE items with open questions** — Source experiment completed but the queue item's question wasn't fully answered. Requires reading experiment summary to verify.

3. **DONE items with partial coverage** — Source experiment partially addressed the question. Lower priority unless the uncovered aspect is high-value.

4. **RUNNING items** — SKIP. Source experiment is still running. The item will resolve naturally when the experiment completes and synthesis processes it.

5. **RESOLVED items** — SKIP. Already tagged by synthesis worker.

## Decision Tree

```
Queue Item
    │
    ├─ Contains "RESOLVED"? → SKIP
    │
    ├─ Has source experiment ID?
    │   │
    │   ├─ NO → NO_SOURCE → CREATE TASK (highest priority)
    │   │
    │   └─ YES → Check source status
    │       │
    │       ├─ Source in running_exp_ids? → RUNNING → SKIP
    │       │
    │       ├─ Source in done_exp_ids? → DONE → Verify if question answered
    │       │   │
    │       │   ├─ Question answered → SKIP (should be tagged RESOLVED)
    │       │   │
    │       │   └─ Question open → CREATE TASK
    │       │
    │       └─ Source not found → UNKNOWN → CREATE TASK (investigate)
    │
    └─ Coverage scoring against running tasks
        │
        ├─ HIGH coverage (>0.3) → SKIP (likely covered)
        │
        ├─ MED coverage (0.15-0.3) → LOWER PRIORITY
        │
        └─ LOW coverage (<0.15) → CREATE TASK
```

## Coverage Scoring

For NO_SOURCE items, compute coverage against running task titles:

```python
stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'from'}

def compute_coverage(item_text, running_titles):
    words = set(item_text.lower().split())
    keywords = {re.sub(r'^exp_\d+\w*$', '', w) for w in words - stop 
                if not re.match(r'^exp_\d+\w*$', w)}
    
    running_text = ' '.join(t.lower() for t in running_titles)
    overlap = sum(1 for w in keywords if w in running_text)
    return overlap / max(len(keywords), 1)
```

Thresholds:
- **HIGH** (>0.3): Likely covered by running experiments. Skip.
- **MED** (0.15-0.3): Partially covered. Lower priority.
- **LOW** (<0.15): Genuinely uncovered. Create task.

## Example Classification (Cycle #212)

```
[34] (NO_SOURCE) Can anti-sycophancy framing generalize to mimo-v2.5? → CREATE
[35] (NO_SOURCE) Does PLATFORM 88.9% ceiling break with larger ensemble? → CREATE
[38] (DONE) Augmentation paradox non-monotonic — oscillation pattern? → CREATE (question open)
[40] (DONE) GDPR 3x less vulnerable — why? → CREATE (question open)
[0] (RUNNING) Q38 asyncio erosion resistance → SKIP (exp_695 running)
[9] (RUNNING) Anti-sycophancy framing for multi-turn → SKIP (exp_714 running)
```

## Pitfall: DONE Category Conflation

The DONE category conflates two situations:
1. **Question answered** — Source experiment completed AND addressed the queue item's question. Should be tagged RESOLVED.
2. **Topic touched** — Source experiment completed but only touched the same topic without addressing the specific question. Still a candidate for new tasks.

**Detection**: After classifying as DONE, read the experiment summary to verify if the question was actually answered. Don't assume DONE means RESOLVED.

## Pitfall: NO_SOURCE Overlap with Running Experiments

NO_SOURCE items can overlap topically with running experiments. The keyword coverage scoring catches these overlaps.

**Example**: Queue item "FDA counter-fact vulnerability — is this domain-specific?" has no source ID, but exp_529 (running) is investigating exactly that question.

**Fix**: Run coverage scoring on NO_SOURCE items against running task titles. Only create tasks for LOW-coverage items.
