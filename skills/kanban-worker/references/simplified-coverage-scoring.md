# Simplified Queue Coverage Scoring

**Added:** Cycle #221
**Purpose:** Quick triage of queue items against running experiments without complex stop-word filtering

## Problem

The quantitative coverage scoring documented in Cycle #177 uses stop words and fraction calculation. This is precise but complex. For quick triage during Director passes, a simpler approach works well enough.

## Solution

Check if 3+ words from the queue item appear in any running task title. No stop word filtering needed.

## Implementation

```python
import json, re

def check_queue_coverage(queue, running_tasks):
    """
    Check which queue items are covered by running experiments.
    
    Args:
        queue: list of queue item dicts (from self_state.json curiosity_queue)
        running_tasks: list of task dicts (from kanban list --json)
    
    Returns:
        list of (index, text, status) tuples
    """
    results = []
    for i, item in enumerate(queue):
        text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
        if 'RESOLVED' in text:
            continue
        
        item_words = set(text.lower().split())
        covered = any(
            len(item_words.intersection(set(t.get('title', '').lower().split()))) >= 3
            for t in running_tasks
        )
        status = "COVERED" if covered else "UNCOVERED"
        results.append((i, text, status))
    
    return results

# Usage in Director pass:
# running = json.load(open('/tmp/kanban_running.json'))
# ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
# queue = ss.get('curiosity_queue', [])
# for idx, text, status in check_queue_coverage(queue, running):
#     print(f"[{idx}] ({status}) {text[:90]}")
```

## Strong-Matches Refinement (added Cycle #239)

After computing coverage scores, MED-coverage items (0.15-0.30) need deeper inspection. Some are genuinely uncovered despite scoring above LOW. The "strong matches" check identifies which MED items have actual topical overlap with running tasks:

```python
# After coverage scoring, check MED items for strong matches
for i, item in enumerate(queue):
    # ... coverage scoring code ...
    if level == "MED":
        matching = []
        for t in running_titles:
            t_words = set(t.lower().split())
            shared = keywords.intersection(t_words)
            if len(shared) >= 2:
                matching.append((t[:60], shared))
        if not matching:
            # MED coverage but no strong matches = genuinely uncovered
            print(f"[{i}] GENUINELY UNCOVERED despite MED score")
```

**Key insight:** A queue item about "Ellipsis_count cross-domain detection" scores MED (0.25) because generic words like "detection" appear in running titles, but no running task shares 2+ specific keywords. This item is genuinely uncovered and deserves a new task.

**When to apply:** After the initial coverage scoring pass, before deciding which items to create tasks for. Items with MED coverage AND no strong matches should be treated as uncovered.

## When to Use

- **Quick triage** during Director passes when you need fast decisions
- **NOT** when you need precise coverage fractions (use Cycle #177 version instead)
- Works well for identifying obviously-covered items (3+ word overlap is strong signal)
- May miss partial matches where only 1-2 words overlap (use Cycle #177 for those)
- **Apply strong-matches refinement** for MED-coverage items to avoid false "covered" classification

## Validation

- Cycle #221: Identified 23/29 active items as covered, 8 as uncovered
- Cycle #239: Strong-matches refinement correctly identified 1 genuinely uncovered MED item (Ellipsis_count cross-domain detection) that would have been missed by keyword scoring alone
- Matches results from the more complex Cycle #177 version in practice
- Faster to implement and debug during Director passes
