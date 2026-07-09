# Synthesis Task Coverage Gap — Queued-but-Uncovered Experiments

**Added:** Cycle #2857
**Category:** Synthesis race condition
**Distinct from:** `post-synthesis-gap-completion-detection.md` (narrow dump→start window)

## Problem

When a synthesis task is created and queued (status=ready), new experiments can complete BEFORE the synthesis worker starts processing. These newly-completed experiments are NOT covered by the queued synthesis task's body, so they remain unsynthesized until the next synthesis cycle.

## Observed Behavior (Cycle #2857)

1. Queued synthesis task `t_26963b9b` covers 24 experiments (exp_2704 through exp_2798)
2. 10 more experiments completed AFTER the task was queued (exp_2751 through exp_2850)
3. Unsynthesized detection: 20 total, only 10 covered by queued synthesis, 10 uncovered
4. Created a second synthesis task `t_dea630b1` for the 10 uncovered experiments

## Root Cause

The synthesis task body lists specific experiments at creation time. But the ready queue is FIFO — the synthesis worker processes tasks in order. If 5+ other tasks are ahead in the queue, the synthesis task may not run for 30-60 minutes. During that window, new experiments complete and aren't covered.

This is distinct from `post-synthesis-gap-completion-detection.md`:
- **Post-synthesis gap**: 1-5 minute window between Director dump and synthesis worker start
- **Queued coverage gap**: 30-60+ minute window while synthesis task waits in ready queue

## Detection

```python
import json, re, sqlite3

# Get completed experiment IDs from kanban
kdb = sqlite3.connect(os.path.expanduser('~/.hermes/kanban.db'))
done_rows = kdb.execute("SELECT id, title FROM tasks WHERE status='done'").fetchall()
done_exp_ids = set()
for row_id, title in done_rows:
    for m in re.finditer(r'exp_(\d+)\b', title):
        done_exp_ids.add(int(m.group(1)))

# Get self_state.json completed IDs
with open(os.path.expanduser('~/.hermes/self_state.json')) as f:
    state = json.load(f)
# ... (use robust access pattern from director-loop pitfalls) ...
ss_exp_ids = set()  # populated from state

unsynthesized = done_exp_ids - ss_exp_ids

# Check what the queued synthesis task covers
queued_synth_body = "..."  # from kanban show
synth_covered = set(int(x) for x in re.findall(r'exp_(\d+)', queued_synth_body))
uncovered = unsynthesized - synth_covered

if len(uncovered) >= 3:
    print(f"COVERAGE GAP: {len(uncovered)} experiments not covered by queued synthesis")
    print(f"  IDs: {sorted(uncovered, reverse=True)}")
    # Create a second synthesis task for the uncovered experiments
```

## Fix

Create a second synthesis task specifically for the uncovered experiments:

```bash
hermes kanban create "SYNTHESIS: consolidate N experiments (exp_A, exp_B, ...) — not covered by queued synthesis t_XXXX" \
  --assignee prometheus-synthesis \
  --body "SYNTHESIS TASK — these experiments are NOT covered by the already-queued synthesis task (t_XXXX).
After the queued synthesis completes, process these next.

Experiments to consolidate:
- exp_A, exp_B, exp_C, ...

Steps:
1. Read self_state.json FIRST (it's ground truth)
2. For each experiment, read results from kanban task summaries
3. Write your output via write_synthesis_output.py (DO NOT write to self_state.json directly):
   python3 ~/.hermes/scripts/write_synthesis_output.py \
     --task-id $HERMES_KANBAN_TASK \
     --experiments "exp_NNN,..." \
     --resolutions '[{"item": N, "resolved_by": "exp_XXX", "note": "..."}]' \
     --curiosities "Follow-up 1?;Follow-up 2?"
   This writes to the synthesis_outputs table. synthesis_merger.py (cron 2m) applies to self_state.json."
```

## Prevention

**Option 1: Batch synthesis tasks** — Instead of creating one synthesis task per cycle, batch all unsynthesized experiments into a single task. This reduces the window for coverage gaps.

**Option 2: Re-check before dispatch** — After creating a synthesis task, immediately re-check unsynthesized count. If it increased (new experiments completed during task creation), add them via `kanban_comment`.

**Option 3: Periodic synthesis trigger** — Run a lightweight cron job every 5 minutes that checks unsynthesized count and creates a synthesis task if ≥5 experiments are uncovered. This catches gaps regardless of when they occur.

## When This Matters Most

- High-throughput periods (many workers completing experiments fast)
- Long synthesis queue (many tasks ahead of the synthesis task)
- After large batch creation events (10+ experiments completing near-simultaneously)

## Integration with Director Flowchart

Add after Step 3 (DETECT UNSYNTHESIZED) in the Director Quick-Pass Flowchart:
```
3b. CHECK QUEUED SYNTHESIS COVERAGE
    If a synthesis task is already queued (ready status):
      Extract experiment IDs from its body
      Compare against unsynthesized set
      If uncovered count ≥ 3:
        Create a SECOND synthesis task for uncovered experiments
```
