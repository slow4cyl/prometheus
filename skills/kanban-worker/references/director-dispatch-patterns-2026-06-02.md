# Director Dispatch Patterns — June 2026

## Worker "script-write-but-never-run" dispatch loop

Workers write Python scripts to the workspace but never execute them, causing the task to stay in "ready" (or get reclaimed) and be re-dispatched indefinitely. Each cycle wastes a worker slot and API credits.

**Observed:** 7 tasks with 2 prior runs each, all with 0 events, all stuck at the same point — script written but never run.

**Detection:** Check workspace files — if only the `.py` script exists with no output/results files after 60+ minutes, the worker wrote but didn't run.

**Fix in task body:** Include explicit instruction:
```
After writing the experiment script to the workspace, IMMEDIATELY run it with
`terminal(background=true, notify_on_complete=true)`. Do NOT stop at writing
the script — execution is the goal.
```

**Fix for existing stuck tasks:** Block them (`kanban_block` with reason noting the pattern) to stop the dispatch loop. The Director should verify via workspace file inspection before blocking.

## Synthesis completion without kanban_complete

A synthesis worker can complete all its work (self_state.json updated, counters reconciled, knowledge graph updated) but never call `kanban_complete` before timing out or being reclaimed. The task stays in "ready" status and gets re-dispatched, wasting another worker on the same already-completed work.

**Detection:** Check `self_state.json` version and `last_updated` timestamp — if they're recent and match the expected synthesis output, the work is done regardless of kanban status.

**Fix:** Manually complete the task:
```bash
hermes kanban complete <task_id> --summary "Synthesis completed: [details]"
```
Do NOT re-run the synthesis — the data is already in self_state.json.

**Prevention:** The synthesis worker's task body should include: "After updating self_state.json, call kanban_complete before doing anything else."

## batch_create_tasks.py min-score threshold gap

The batch creator defaults to min-score=60, but many valid queue items score 60-73 with the curiosity scorer. When the batch script creates only 2 tasks but 50 workers are free, the Director should manually create tasks for remaining high-value items.

**Workaround:** After running batch_create_tasks.py, check how many free workers remain. If >5 free workers and >5 active queue items exist, manually create tasks for the top-scored uncovered items using individual `hermes kanban create` calls (or a Python script with subprocess). Assign to specific free worker profiles.

Example:
```python
import subprocess, json, os, re

items = [
    (40, "ML routing generalization question..."),
    (36, "Feature reduction question..."),
    # ... more items
]

workers = [f"prometheus-worker-{i}" for i in range(1, 23)]
free_workers = [w for w in workers if w != "prometheus-worker-2"]  # exclude busy

for i, (idx, text) in enumerate(items):
    if i >= len(free_workers):
        break
    worker = free_workers[i]
    title = f"exp_AUTO: [{text[:70]}]"
    body = f"HYPOTHESIS: {text}\n\nMETHOD:\n1. Design and run experiment\n2. Use mimo-v2.5 via OpenRouter\n..."
    cmd = ["hermes", "kanban", "create", title, "--assignee", worker, "--body", body]
    subprocess.run(cmd, capture_output=True, text=True, timeout=30)
```

## Gateway dispatch timing

The gateway's embedded dispatcher ticks every 60 seconds. After creating tasks, wait at least 15 seconds before checking if they moved to "running". The `hermes kanban dispatch` CLI command triggers an immediate tick but may show "Spawned: 0" if the gateway's embedded dispatcher already picked them up.

**Verification:** After dispatch, check `hermes kanban list --status running --json` to confirm tasks moved to running. If still "ready" after 60 seconds, check gateway logs for errors.
