# Director Batch Desync Verification (added Cycle #263)

## Problem

`kanban list --status running --json` can include tasks that are actually `done` (Variant 1 desync). This inflates the saturation count, hides experiments from unsynthesis detection, and can cause the Director to skip creating experiment tasks when workers are actually free.

In Director #263, 4 of 14 listed-running tasks were actually done — true running count was 10, not 14. Without batch verification, the Director would have applied synthesis-only mode incorrectly (or correctly for the wrong reason — the true count of 10 is still above saturation, but the gap matters when counts are near the threshold).

## Batch Verification Pattern

After dumping board state (step 1), verify every task in the running list:

```python
import json, subprocess

running = json.load(open('/tmp/kanban_running.json'))
desynced_done = []
truly_running = []

for t in running:
    tid = t['id']
    result = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'],
                          capture_output=True, text=True, timeout=10)
    try:
        data = json.loads(result.stdout)
        actual_status = data.get('task', {}).get('status', '?')
        if actual_status == 'done':
            desynced_done.append((tid, t.get('title', '')))
        else:
            truly_running.append(tid)
    except:
        truly_running.append(tid)  # assume running if parse fails

print(f"Listed: {len(running)}, Actually running: {len(truly_running)}, Desynced done: {len(desynced_done)}")
for tid, title in desynced_done:
    print(f"  DESYNCED: {tid}: {title[:60]}")
```

## Integration with Director Flow

This check must happen **between step 1 (dump board state) and step 3 (unsynthesis detection)**. The desynced-done tasks need to be:

1. **Removed from the running set** — don't count them toward saturation
2. **Added to the unsynthesis set** — their experiment IDs need synthesis (Variant 5)
3. **NOT added as new experiment tasks** — they're already done

```python
# After batch verification, adjust the running set
running_ids = set(truly_running)  # excludes desynced-done
desync_exp_ids = set()
for tid, title in desynced_done:
    for m in re.finditer(r'exp_(\d+)', title):
        desync_exp_ids.add(f'exp_{m.group(1)}')

# Add desynced experiments to the unsynthesis set
unsynth = done_exp_ids.difference(ss_exp_ids).union(desync_exp_ids)
```

## When to Skip Batch Verification

- **<5 running tasks**: Desync impact is minimal, individual checks are fast enough
- **Just verified in this pass**: Don't re-verify if you already checked all tasks (e.g., during stuck worker diagnosis)

## Performance Note

For 14+ running tasks, the batch verification adds ~14 sequential `kanban show` calls (~10s each = ~2.3min total). This is acceptable because:
- It runs once per Director pass
- The alternative (wrong saturation decision) costs an entire wasted cycle
- The calls can be parallelized with `&` and `wait` in bash if needed

## Relationship to Existing Variants

This pattern implements the detection side of Variant 5 (desynced done tasks invisible to unsynthesis detection). The variant document describes the problem; this reference provides the batch solution that should be standard in every Director pass.
