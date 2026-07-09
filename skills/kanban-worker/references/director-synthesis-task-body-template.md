# Director Pass Template — Synthesis Task Body with Counter Drift Fix

**Added:** Cycle #198
**Category:** Director workflow template

When the Director detects counter drift (metrics counters != len(experiments.completed)), the synthesis task body should explicitly instruct the worker to fix it. Here's the pattern used in Cycle #198:

## Template

```
SYNTHESIS CYCLE: Read self_state.json FIRST (source of truth). Then synthesize these N experiments:

exp_XXX: <one-line summary>
exp_YYY: <one-line summary>
...

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \
  --task-id $HERMES_KANBAN_TASK \
  --experiments "exp_NNN,..." \
  --resolutions '[{"item": N, "resolved_by": "exp_XXX", "note": "..."}]' \
  --curiosities "Follow-up 1?;Follow-up 2?"
This writes to the synthesis_outputs table. synthesis_merger.py (cron 2m) applies to self_state.json.

CRITICAL: Read self_state.json FIRST (source of truth). Do not run experiments. Write via write_synthesis_output.py — do NOT write to self_state.json directly.
```

## Key elements
1. Explicitly state current array length vs counter value (e.g., "515 entries but counters say 521")
2. List ALL counter fields that must be set to `len(completed)`
3. Remind worker to add experiments as DICTS to the array, not just update purpose text
4. Include version number so worker increments correctly

## Why this works
The synthesis worker often focuses on the narrative (purpose text) and forgets the structural (completed array). By explicitly calling out the drift and listing the exact fix, the Director reduces the chance of the worker repeating the same omission.
