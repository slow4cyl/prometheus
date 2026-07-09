# Same-ID Different-Topic Collision (added Cycle #246)

## Problem

Two tasks share the same experiment ID prefix but investigate completely different topics. The ID-based duplicate detection flags them as duplicates even though they're different experiments.

## Example (Cycle #246)

- **Done task** t_63b08720: `exp_1484: Predict optimal stopping points BEFORE training using domain character`
- **Running task** t_55f2da66: `exp_1484: INT1 binary edge deployment — is 82.3pp accuracy on RPi5 usable for light`

Same ID (`exp_1484`), completely different topics. The standard duplicate detection (`re.findall(r'exp_(\d+)', title)` → intersection) would flag these as duplicates and incorrectly reclaim the running task.

## Detection

After ID-based intersection check, compare the topic text for each pair of matched IDs:

```python
import re
for exp_id in intersection:
    done_topics = [re.search(r'exp_\d+:\s*(.+)', t['title']).group(1)
                   for t in done_tasks if exp_id in t.get('title', '')]
    running_topics = [re.search(r'exp_\d+:\s*(.+)', t['title']).group(1)
                      for t in running_tasks if exp_id in t.get('title', '')]
    for dt in done_topics:
        for rt in running_topics:
            overlap = len(set(dt.split()) & set(rt.split())) / max(len(set(dt.split())), 1)
            if overlap < 0.5:  # Topics differ significantly
                print(f"COLLISION: {exp_id} — different topics, NOT a duplicate")
```

## Fix

Do NOT reclaim the running task. Let both complete. The synthesis worker processes them independently.

Note the collision in the Director audit log for tracking.

## Root Cause

Director task creation error — same experiment number assigned to two different investigations.

## Prevention

Director should check `hermes kanban list --status done --json` for existing experiment IDs before creating new tasks. If an ID already exists in done tasks, increment the experiment counter before creating the new task.
