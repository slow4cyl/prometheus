# Experiment ID Reuse Across Director Passes (added 2026-06-01)

## Problem

The same experiment ID (e.g., `exp_954`) can be assigned to two *different* experiments across separate Director passes — one completed, one running. This is distinct from "duplicate dispatch" (same experiment re-dispatched).

### Observed case (2026-06-01 Director pass)

- **Done task** t_da78e6f1: `exp_954: Contradiction probing fails on dialogue format — alternative detection`
- **Running task** t_a395e256: `exp_954: Output length ratio as pre-filter feature`

Same ID, completely different titles and hypotheses.

### Root cause

Director passes use a local experiment ID counter that can go stale. The `max(all_exp_ids) + 1` pattern from the director-loop skill's "Director Experiment ID Management" section only scans kanban task titles — but completed experiments may not all appear in task titles (synthesis tasks use range notation like "exp_971-999", and some experiments are synthesized without individual task creation).

## Detection

When extracting experiment IDs from done+running intersection:

```python
for exp_id in duplicates:
    done_title = [t['title'] for t in done if exp_id in t.get('title','') and not is_synth(t)][0]
    running_title = [t['title'] for t in running if exp_id in t.get('title','')][0]
    
    # Normalize and compare core topic (after the exp_NNN: prefix)
    done_topic = re.sub(r'^exp_\d+\w*:\s*', '', done_title)[:50].lower()
    running_topic = re.sub(r'^exp_\d+\w*:\s*', '', running_title)[:50].lower()
    
    if done_topic != running_topic:
        print(f"ID REUSE: {exp_id} — done='{done_topic}' vs running='{running_topic}'")
        # Do NOT reclaim — different experiments
```

## Fix

Before creating experiment tasks, always check:
1. `self_state.json` → `experiments.completed` array for max experiment number
2. `self_audit.log` for the most recent experiment ID used
3. Cross-reference against kanban task titles

The correct pattern:
```python
import re, json, os

# From self_state.json
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else str(e)
    ss_exp_ids.add(eid)

# From kanban done list
done_exp_ids = set()
for t in done:
    for m in re.finditer(r'exp_(\d+\w*)', t.get('title', '')):
        done_exp_ids.add(f'exp_{m.group(1)}')

# Union of all known IDs
all_exp_ids = ss_exp_ids.union(done_exp_ids)
max_num = max((int(re.search(r'(\d+)', e).group(1)) for e in all_exp_ids if re.search(r'(\d+)', e)), default=0)
next_exp_id = max_num + 1
```

## Contrast with "Duplicate dispatch"

| Pattern | What it is | Action |
|---------|-----------|--------|
| Duplicate dispatch | Same experiment re-dispatched (same title/hypothesis) | Reclaim the newer task |
| ID reuse | Same ID for different experiments (different titles) | Do NOT reclaim — it's a different experiment |
| Topic duplicate | Different IDs for same research question | Reclaim newer, block with duplicate reason |
