# Synthesis Task Body Coverage Verification

**Added: Cycle #245 (June 2026)**

## Problem

When the Director creates a synthesis task, the body lists experiments to synthesize. But this list can be stale or incomplete — it may include experiments that are NOT actually unsynthesized, or MISS experiments that ARE unsynthesized.

In Cycle #245, the synthesis task listed 13 experiments, but 2 unsynthesized experiments (exp_2617, exp_2637) were absent from the list. These experiments were in `done_exp_ids - ss_exp_ids` but not in the synthesis task body.

## Root Cause

The synthesis task body is generated from a stale scan or from a different detection method than the one used to identify unsynthesized experiments. The Director may create the task before completing the full unsynthesis detection, or the detection code and the task body generator use different data sources.

## Verification Step

After creating the synthesis task, BEFORE dispatching:

```python
import json, re, os

# 1. Get actual unsynthesized set (Method A)
done = json.load(open('/tmp/kanban_done.json'))
done_exp_ids = set()
for t in done:
    title = t.get('title', '')
    if 'synth' not in title.lower() and 'consolidat' not in title.lower():
        for m in re.finditer(r'exp_(\d+)', title):
            done_exp_ids.add(f'exp_{m.group(1)}')

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    if isinstance(e, dict):
        ss_exp_ids.add(e.get('id', ''))
    elif isinstance(e, str):
        m = re.search(r'exp_(\d+)', e)
        if m: ss_exp_ids.add(f'exp_{m.group(1)}')

unsynth = done_exp_ids - ss_exp_ids

# 2. Extract experiments from synthesis task body
synth_body = "..."  # from kanban_show --json
listed_exp_ids = set()
for m in re.finditer(r'exp_(\d+)', synth_body):
    listed_exp_ids.add(f'exp_{m.group(1)}')

# 3. Find missing
missing = unsynth - listed_exp_ids
if missing:
    print(f"MISSING from synthesis body: {missing}")
    # Add via kanban_comment
```

## Fix Pattern

Use `kanban_comment` to add missing experiments:

```bash
hermes kanban comment <synthesis_task_id> "ADDITIONAL UNSYNTHESIZED EXPERIMENTS NOT IN ORIGINAL LIST:
exp_NNN: <one-line summary>
exp_MMM: <one-line summary>

Please include these in the synthesis pass."
```

## When to Check

- After creating the synthesis task
- Before dispatching (or right after dispatch, before the synthesis worker starts processing)
- The synthesis worker typically completes in <2 minutes, so check event count before commenting (0 events = safe to comment)

## See Also

- `references/synthesis-scope-expansion-0-events-check.md` — timing of comments on synthesis tasks
- Director Synthesis workflow in main SKILL.md — Step 1 (read completed task summaries)
