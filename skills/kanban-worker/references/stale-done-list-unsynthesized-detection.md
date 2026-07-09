# Stale Done-List Pitfall for Unsynthesized Detection (Cycle #247)

## Problem

The Director's unsynthesized detection (Method A: `done_exp_ids - ss_exp_ids`) uses a done-list dumped at the START of the pass:

```bash
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json
```

Experiments that complete DURING the pass are missing from this snapshot. If an experiment completes after the dump but before unsynthesized detection runs, the script reports N-1 unsynthesized instead of N.

## Impact

This only matters at the ≥3 threshold boundary:
- **N=2 → reports 1**: Director skips synthesis (below threshold). Should have created synthesis.
- **N=4 → reports 3**: Still ≥3, synthesis is created. No impact.

## Observed in Cycle #247

- exp_1477 completed DURING the Director pass (status desync: list showed running, show confirmed done)
- Done-list was dumped at start of pass — exp_1477 not yet in it
- Unsynthesized detection found only exp_1475 (1 total, below ≥3 threshold)
- Actual count was 2 (exp_1475 + exp_1477)
- Director correctly skipped synthesis this time (2 < 3), but the count was wrong

## Fix

After the initial done-list dump, re-check before running unsynthesized detection:

```python
# Option 1: Re-dump done list after task creation
subprocess.run(["hermes", "kanban", "list", "--status", "done", "--json"],
               stdout=open("/tmp/kanban_done.json", "w"), timeout=15)
# Then re-run unsynthesized detection

# Option 2: Merge new IDs incrementally
new_done = json.load(open("/tmp/kanban_done.json"))
new_exp_ids = set()
for t in new_done:
    for m in re.finditer(r'exp_(\d+\w*)', t.get('title', '')):
        new_exp_ids.add(f'exp_{m.group(1)}')
# Add any new IDs not in original set
```

## Detection

Compare done-list count before and after task creation. If it increased, re-run unsynthesized detection.

## When This Matters

- Long Director passes where experiments are likely to complete mid-pass
- When unsynthesized count is near the ≥3 threshold (2 or 3)
- When running tasks are near completion (old tasks, >60m)
