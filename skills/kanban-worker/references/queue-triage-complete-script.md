# Complete Queue Triage Script (Director Pass #250)

Single-script queue classification + coverage scoring + genuinely-open identification. Run after dumping board state to `/tmp/kanban_running.json` and `/tmp/kanban_done.json`.

```python
import json, re, os

# Load board state
running = json.load(open('/tmp/kanban_running.json'))
running_exp_ids = set()
running_titles = []
for t in running:
    title = t.get('title', '')
    running_titles.append(title)
    for m in re.finditer(r'exp_(\d+\w*)', title):
        running_exp_ids.add('exp_%s' % m.group(1))

# Load self_state and queue
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if not eid and isinstance(e, str):
        m = re.search(r'exp_(\d+\w*)', e)
        if m:
            eid = 'exp_%s' % m.group(1)
    if eid:
        ss_exp_ids.add(eid)

# Coverage scoring stop words
stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'from'}

# Classify each queue item
open_items = []
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue

    # Source-based classification
    source_ids = re.findall(r'exp_(\d+\w*)', text)
    if not source_ids:
        category = "NO_SOURCE"
    elif any('exp_%s' % sid in running_exp_ids for sid in source_ids):
        category = "RUNNING"
    elif any('exp_%s' % sid in ss_exp_ids for sid in source_ids):
        category = "DONE"
    else:
        category = "UNKNOWN"

    # Skip RUNNING items (will resolve naturally)
    if category == "RUNNING":
        continue

    # Coverage scoring against running task titles
    words = set(text.lower().split())
    keywords = {w for w in words - stop if not re.match(r'^exp_\d+\w*$', w) and len(w) > 2}
    running_text = ' '.join(t.lower() for t in running_titles)
    overlap = sum(1 for w in keywords if w in running_text)
    coverage = overlap / max(len(keywords), 1)

    # Only genuinely open items (coverage < 0.3)
    if coverage < 0.3:
        open_items.append((i, category, coverage, text))

# Report
print("=== GENUINELY OPEN QUEUE ITEMS ===\n")
for idx, cat, cov, text in open_items:
    print("[%d] (%s, cov=%.2f) %s" % (idx, cat, cov, text[:120]))
print("\nTotal open items:", len(open_items))
print("\nNext: Create experiment tasks for these items, prioritizing NO_SOURCE > DONE/LOW")
```

## Usage in Director Pass

1. Dump board state: `hermes kanban list --status {running,done} --json > /tmp/kanban_{running,done}.json`
2. Run this script
3. Create tasks for the output items (assign to free workers)
4. Items with coverage < 0.15 are HIGH priority (completely uncovered)
5. Items with coverage 0.15-0.30 are MEDIUM priority (partially covered)

## Threshold rationale

- Coverage < 0.30 = genuinely open (used in Cycle #250, captured 15 items from 31)
- Coverage 0.15-0.30 = partially covered by running experiments
- Coverage > 0.30 = likely covered (skip)
