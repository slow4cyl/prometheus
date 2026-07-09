# Pitfall: Queue Items Referencing Unsynthesized Experiments

**Added:** Cycle #239  
**Category:** Director queue triage  
**Severity:** Medium — causes wasted workers on items that synthesis will resolve

## Problem

When a queue item's source experiment is in `done_exp_ids` but NOT in `self_state.json`'s `experiments.completed`, the item references an experiment that completed but hasn't been synthesized yet. Creating a new task for such an item is premature — synthesis will process the experiment's results and may resolve or clarify the queue item.

## Detection

```python
import json, re, os

done = json.load(open('/tmp/kanban_done.json'))
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))

# Get unsynthesized experiment IDs
done_exp_ids = set()
for t in done:
    title = t.get('title', '')
    is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
    if not is_synth:
        for m in re.finditer(r'exp_(\d+\w*)', title):
            done_exp_ids.add('exp_%s' % m.group(1))

ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if eid:
        ss_exp_ids.add(eid)

unsynth = done_exp_ids - ss_exp_ids

# Check queue items against unsynthesized experiments
queue = ss.get('curiosity_queue', [])
for item in queue:
    if isinstance(item, dict):
        text = item.get('text', str(item))
    else:
        text = str(item)
    if 'RESOLVED' in text:
        continue
    
    source_ids = re.findall(r'exp_(\d+\w*)', text)
    for sid in source_ids:
        if 'exp_%s' % sid in unsynth:
            print("DEFER: %s (source exp_%s is unsynthesized)" % (text[:60], sid))
            break
```

## Resolution

1. Do NOT create a new task for the deferred item
2. Note the deferred item in the synthesis task body as a follow-up to investigate AFTER synthesis
3. After synthesis completes, re-check the queue item — synthesis may have tagged it RESOLVED or provided enough context to determine if a new task is needed

## Real-World Example (Cycle #239)

Queue had 13 active items. Items 14-17 referenced exp_1146, exp_1148, exp_1152, exp_1156 — all completed but unsynthesized. These 4 items were deferred. Items 5-13 referenced fully-synthesized experiments and were tasked immediately.

## Related

- See "DONE category conflates 'answered' vs 'touched topic'" pitfall (Cycle #187) for the broader DONE classification issue
- See "Recheck unsynthesis after recent synthesis" section in `references/director-queue-triage-cycle201.md` for when to re-evaluate deferred items
