# Synthesis Race Condition — Detection & Recovery Workflow

**Problem:** When the Director adds experiments to a running synthesis task via `kanban_comment`, the synthesis may complete before the comment is processed. The synthesis worker never sees the comment, and the experiments are missed.

**Timeline:** Synthesis workers complete in <3 minutes (0 heartbeats is normal). The comment arrives after completion.

## Mandatory Detection Pattern

After every `kanban_comment` on a synthesis task, IMMEDIATELY re-check status:

```bash
hermes kanban show <synthesis_task_id> --json 2>&1 > /tmp/synth_check.json
python3 -c "import json; d=json.load(open('/tmp/synth_check.json')); print(d.get('task',{}).get('status','?'))"
```

If `status=done`, the comment was too late. Proceed to recovery.

## Recovery: Create Dedicated Synthesis Task

Do NOT try to re-open the completed task. Create a new one:

```bash
hermes kanban create "SYNTHESIS: consolidate <missed_exp_ids>" \
  --assignee prometheus-synthesis \
  --body "SYNTHESIS CYCLE: Read self_state.json first (source of truth). Synthesize <exp_ids>. Write output via write_synthesis_output.py (DO NOT write to self_state.json directly). synthesis_merger.py (cron 2m) applies to self_state.json."
```

## Prevention: Avoid the Race Entirely

**Rule (Cycle #246+):** After dispatching a synthesis task, the Director should NOT attempt to expand its scope via `kanban_comment` in the same pass. If you discover additional unsynthesized experiments after dispatching synthesis, create a SEPARATE synthesis task for them from the start.

**Why:** The synthesis worker is too fast (<3 min) for the comment to arrive in time. The only reliable signal is `kanban show --json` reporting `status=done`, but by then it's too late to comment.

**Correct Director workflow:**
1. Identify ALL unsynthesized experiments BEFORE dispatching synthesis
2. List them ALL in the initial synthesis task body
3. If new experiments complete AFTER dispatch, create a SECOND synthesis task (don't comment on the first)
4. Never assume a running synthesis task will read your comment

## Real-World Examples

- **Cycle #188:** exp_530 added to t_66c61c6a via comment -> synthesis already done -> exp_530 missed
- **Cycle 246+:** exp_1507 added to t_6c669e8d via comment -> synthesis completed in 3 min -> exp_1507 missed -> created t_49f845bd as dedicated synthesis

## Key Insight

The race condition is structural: synthesis workers are faster than the comment delivery pipeline. No amount of timing optimization fixes it. The only reliable approach is to either (a) include all experiments in the initial task body, or (b) create separate tasks for late discoveries.
