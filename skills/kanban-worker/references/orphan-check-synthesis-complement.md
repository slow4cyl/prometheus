# Orphan Check as Synthesis Complement

## Problem

`kanban list --status done` has a **propagation delay** — tasks that just completed via `kanban_complete` may not appear in the done list for 1-3 minutes. During this window, the unsynthesized detection algorithm (Method A: `done_exp_ids - ss_exp_ids`) misses these experiments because they're not in the done list yet.

Meanwhile, the task's process has exited (the hermes agent called `kanban_complete` and terminated), so the task appears as an "orphan" — in the running list but with no visible process in `ps aux`.

## Detection Pattern

Run the orphan check as part of every Director pass, BEFORE the unsynthesized detection:

```python
import subprocess, re, json

# Get running task IDs
running = json.load(open('/tmp/kanban_running.json'))
running_ids = {t['id']: t.get('title', '') for t in running}

# Get all task IDs from ps aux
ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
ps_tasks = set()
for line in ps_result.stdout.split('\n'):
    m = re.search(r'kanban task (t_\w+)', line)
    if m:
        ps_tasks.add(m.group(1))

# Find tasks with no visible process
orphans = set(running_ids.keys()) - ps_tasks
```

For each orphan, check actual status:

```python
for tid in orphans:
    result = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'],
                          capture_output=True, text=True, timeout=10)
    d = json.loads(result.stdout)
    status = d.get('task', {}).get('status', '?')
    if status == 'done':
        # This task completed but hasn't moved to the done list yet
        # Extract experiment ID from title and add to synthesis
        title = running_ids[tid]
        exp_match = re.search(r'exp_(\d+\w*)', title)
        if exp_match:
            print(f"ORPHAN-COMPLETED: {tid} = exp_{exp_match.group(1)} — add to synthesis")
```

## Integration with Director Pass

After the orphan check, add any newly-discovered completed experiments to the running synthesis task via `kanban_comment`:

```bash
hermes kanban comment <synthesis_task_id> "ADDITIONAL: Also synthesize exp_XXX and exp_YYY: <one-line summaries>"
```

If no synthesis task exists yet, include these experiments in the synthesis task body when creating it.

## Why This Matters

In the session where this was discovered (Cycle #224), exp_886 and exp_910 had completed but weren't in the done list. The unsynthesized detection found 9 experiments. The orphan check caught 2 more (exp_886, exp_910) that would have been missed. Without this pattern, those experiments would remain unsynthesized until the next Director pass (15+ minutes later).

## Relation to Existing Patterns

- **Complements** unsynthesized detection (Method A): catches the propagation delay gap
- **Distinct from** zombie detection: zombies are processes alive for completed tasks; orphans are processes dead for running-list tasks
- **Distinct from** stuck worker protocol: orphans are NOT stuck — they're completed. The process exited cleanly after `kanban_complete`.
