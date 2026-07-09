# Synthesis "Phantom Reference" Misclassification (Cycle #229)

## Problem

The synthesis worker can incorrectly classify REAL completed tasks as "phantom references" — claiming they were "never actually conducted" when they DO exist in the done list with status=done.

## Example (Cycle #229)

Synthesis task t_7d741309 was created with body: "SYNTHESIS: exp_955 + exp_997 (unsynthesized)"

The synthesis worker:
1. Looked up exp_955 and exp_997 in self_state.json
2. Didn't find them (because they hadn't been synthesized yet — that's why they were unsynthesized)
3. Concluded they were "phantom references in the curiosity queue"
4. Synthesized "related experiments" instead of the actual experiments

Both exp_955 (t_ef1be189) and exp_997 (t_2ba5071e) were REAL done tasks with complete results.

## Root Cause

The synthesis worker interprets "unsynthesized" as "might not exist" rather than "exists but hasn't been consolidated into self_state.json yet." When it can't find the experiment in self_state.json, it assumes the experiment is a phantom rather than an unprocessed done task.

## Detection

After a synthesis task completes, cross-reference its summary against the done list:
```bash
hermes kanban list --status done --json > /tmp/kanban_done.json
# Check if summary mentions "phantom" or "never conducted" for experiment IDs
# that HAVE done tasks in the list
```

## Fix

Create a new synthesis task that explicitly includes the kanban task ID alongside the experiment ID:

```
SYNTHESIS: exp_955, exp_997, exp_1002 (missed by prior synthesis)

Experiments to synthesize:
- exp_955 (t_ef1be189): Native knowledge confidence as universal vulnerability predictor
- exp_997 (t_2ba5071e): Named citations 53pp deference drop -- safe citation format?
- exp_1002 (t_dafb4115): Semantic citation detection + contradiction probing -- reduce FPR?
```

Including the task ID (t_xxxxxxxx) proves the experiment exists and prevents the synthesis worker from dismissing it.

## Prevention

When creating synthesis tasks, ALWAYS include the kanban task ID alongside the experiment ID. Format: `exp_NNN (t_xxxxxxxx): [one-line title]`. This gives the synthesis worker a concrete reference to verify against the kanban database, rather than relying solely on self_state.json lookup.
