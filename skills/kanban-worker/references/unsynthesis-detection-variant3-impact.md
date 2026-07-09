# Variant 3 Impact on Unsysis Detection (Cycle #239)

## Problem

Method A unsynthesis detection (main SKILL.md "Identify unsynthesized experiments" section) uses `kanban list --status done` to extract experiment IDs from done task titles, then subtracts `self_state.json`'s `experiments.completed` array. The result is the set of experiments needing synthesis.

**Variant 3 list omission** (see `status-desync-variants.md`) means some actually-running tasks are MISSING from `kanban list --status running`. These tasks may also have prior done-list entries (from previous attempts). The result:

1. The experiment ID appears in `done_exp_ids` (from the done list)
2. The experiment ID is NOT in `ss_exp_ids` (not yet synthesized)
3. The experiment ID is NOT in `running_exp_ids` (missing from running list due to Variant 3)
4. **False positive**: experiment flagged as "needs synthesis" when it's actually being worked on by a live worker

## Detection

After computing `unsynth = done_exp_ids - ss_exp_ids`, cross-reference each unsynthesized experiment against `ps aux` output:

```python
import subprocess, re

# Get actually-running task IDs from ps aux (ground truth)
ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
actually_running = set()
for line in ps_result.stdout.split('\n'):
    m = re.search(r'kanban task (t_\w+)', line)
    if m:
        actually_running.add(m.group(1))

# Get experiment IDs from actually-running tasks
running_exp_ids_from_ps = set()

# Check tasks in kanban list
for t in running:  # from kanban list --status running --json
    if t['id'] in actually_running:
        for m in re.finditer(r'exp_(\d+)', t.get('title', '')):
            running_exp_ids_from_ps.add(f'exp_{m.group(1)}')

# Check tasks NOT in kanban list but present in ps aux (Variant 3 omissions)
listed_ids = {t['id'] for t in running}
for tid in actually_running - listed_ids:
    result = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'],
                          capture_output=True, text=True, timeout=10)
    try:
        d = json.loads(result.stdout)
        title = d.get('task', {}).get('title', '')
        for m in re.finditer(r'exp_(\d+)', title):
            running_exp_ids_from_ps.add(f'exp_{m.group(1)}')
    except:
        pass

# Remove actually-running experiments from unsynthesized set
unsynth = unsynth.difference(running_exp_ids_from_ps)
```

## Real-world example (Cycle #239)

- `kanban list --status running` returned 13 tasks
- `ps aux` showed 14 worker processes (12 matched list, 2 missing: worker-7 on t_f117d40a/exp_1123, worker-10 on t_7bb72837/exp_1126)
- Method A flagged exp_1123 and exp_1126 as "needs synthesis" (they appeared in done list from prior attempts, not in self_state.json, and not in running list)
- Both were actually being worked on by live workers — creating synthesis for them would have been redundant
- After ps aux cross-reference: only exp_1153 genuinely needed synthesis (single experiment, deferred)

## Impact

Without the ps aux cross-reference, the Director may create unnecessary synthesis tasks for experiments that are actively being worked on. This wastes a worker slot and risks concurrent writes to self_state.json (the running experiment will produce new results that need synthesis anyway).

## Integration with Method A

Add the ps aux cross-reference as a final step after computing `unsynth`:

```python
# After: unsynth = done_exp_ids - ss_exp_ids
# Add: cross-reference against ps aux to remove actually-running experiments
unsynth = remove_actually_running(unsynth)  # using the function above
```

This should be applied BEFORE deciding whether to create a synthesis task. If the cross-reference reduces `unsynth` to 0, no synthesis task is needed.
