# Synthesis Phantom Success (added Cycle #219)

## Problem

A synthesis task reaches `status=done` with a `latest_summary` that claims consolidation of experiments, but the experiments are NOT actually present in `self_state.json`'s `experiments.completed` array. The task "succeeded" from kanban's perspective (worker called `kanban_complete`) but the critical side-effect — writing to self_state.json — failed silently.

## Observed: Cycle #219

- Synthesis task t_406ef8e6 completed with summary: "Synthesis v371 complete. exp_843/850/858 consolidated into self_state.json."
- `kanban show --json` confirmed status=done with summary present
- But `self_state.json` → `experiments.completed` array contained ZERO of the 3 experiments
- The standard unsynthesis detection (Method A: done_exp_ids minus ss_exp_ids) caught it immediately

## Why This Happens

Possible causes (not mutually exclusive):
1. **TIRITH filter corruption**: The synthesis worker's `write_file` or Python `json.dump` was filtered by TIRITH
2. **Race condition**: Another process overwrote self_state.json after the synthesis worker wrote it
3. **Worker crash after kanban_complete but before write**: The worker called `kanban_complete` first, then attempted the self_state.json write which failed
4. **Partial write**: The synthesis worker updated some fields but not the `experiments.completed` array

## Detection

The unsynthesis detection in the Director pass already catches this:
```python
unsynth = done_exp_ids - ss_exp_ids
```
If `unsynth` is non-empty and a synthesis task exists for those experiments, the synthesis claimed success but didn't write.

## Fix

1. Create a dedicated SYNTHESIS FIX task (not re-running the original synthesis — just the write)
2. Assign to prometheus-synthesis
3. Task body explicitly lists the missing experiment IDs and their one-line summaries
4. The synthesis worker reads self_state.json FIRST, then writes output via write_synthesis_output.py (synthesis_merger.py applies to self_state.json)

## Prevention

- **Verify synthesis output**: After a synthesis task completes, ALWAYS verify that claimed experiments appear in self_state.json
- **Single-writer invariant**: Only the synthesis worker writes to self_state.json
- **Audit trail**: The synthesis fix should be logged as a separate entry in self_audit.log

## Contrast with Existing Pitfalls

- **Synthesis title/summary claims don't match actual coverage** (Cycle #159): That's about synthesis claiming experiments it never ran. This is about synthesis that DID run and complete, but the write didn't persist.
- **Metrics counter drift** (Cycle #152+): Related — drift happens when writes are partial. Phantom success is a more severe variant where the entire array update is missing.
