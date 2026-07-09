# Mid-Pass Worker Liberation — Re-check Coverage

**Added:** Cycle #245  
**Category:** Director workflow, saturation dynamics  
**Related:** Saturation threshold edge case (Cycle #152), Free workers during saturation (Cycle #166)

## Problem

The Director flowchart's saturation check is evaluated ONCE at the start of a pass. But the board state is dynamic — tasks complete mid-pass, freeing workers. If the Director starts at saturation (≥7 running, 0 free workers) and ends without re-checking, freed workers idle until the next dispatch tick.

## Pattern

```
1. Director pass starts: 9 running, 0 free → saturation mode (synthesis only)
2. exp_1483 completes mid-pass → worker-1 freed
3. Synthesis task completes → another slot freed
4. Director should NOW re-check coverage and create tasks
5. Instead of just logging and ending
```

## Detection

After ANY task completes during a Director pass:

```bash
# Quick worker count
ps aux | grep prometheus-worker | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u | wc -l

# Quick running task count
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running_recheck.json
python3 -c "import json; print(len(json.load(open('/tmp/kanban_running_recheck.json'))))"
```

If `free_workers ≥ 1` AND `LOW-coverage queue items exist` → create tasks before logging the pass.

## Coverage Re-scoring

Re-run the coverage scoring with the UPDATED running task list (not the stale one from pass start):

```python
import json, re

d = json.load(open('/tmp/kanban_running_recheck.json'))  # fresh dump
running_titles = ' '.join(t.get('title', '').lower() for t in d)

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

stop = {'the', 'a', 'an', 'is', 'are', ...}  # full stop word list
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text: continue
    words = set(re.findall(r'\w+', text.lower()))
    keywords = {w for w in words - stop if len(w) > 3 and not re.match(r'^exp_\d+$', w)}
    overlap = sum(1 for w in keywords if w in running_titles)
    coverage = overlap / max(len(keywords), 1)
    if coverage < 0.15:
        print(f'[{i}] LOW ({coverage:.2f}) {text[:100]}')
```

## Real Example (Cycle #245)

| Metric | Pass Start | After Mid-Pass Completions |
|--------|-----------|---------------------------|
| Running tasks | 9 | 7 (then 6 after synthesis) |
| Free workers | 0 | 1 (worker-1 from exp_1483) |
| LOW-coverage items | 2 | 6 (synthesis task completion changed running set) |
| Action | Saturation mode | Created exp_1553 for highest-priority LOW item |

The coverage scoring found MORE LOW items after the synthesis task completed because the synthesis task's title was no longer in the running set, reducing keyword overlap for several queue items.

## Key Insight

**Coverage scoring is sensitive to the running task set.** When a synthesis task completes, queue items that referenced its experiments lose coverage. Re-scoring after synthesis completion often reveals NEW LOW-coverage items that weren't LOW at pass start.

## Anti-pattern

Don't just log "0 free workers, saturation mode" and end the pass. The board state 5 minutes later may be completely different. Always re-check before finalizing.
