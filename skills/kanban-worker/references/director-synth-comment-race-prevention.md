# Director Synthesis Comment Race Condition — Prevention Pattern

## Problem
When the Director adds late-completing experiments to a running synthesis task via `kanban_comment`, the synthesis task may have already completed before the comment arrives. The comment is silently lost — the synthesis worker never sees it.

## Observed in
- Cycle #188: exp_530 added to synthesis t_66c61c6a via comment, but task already done
- Cycle #219: 5 experiments (exp_819, 824, 826, 831, 849) added to synthesis t_21533591 via comment, but synthesis v367 had already completed
- Cycle #246: 7 experiments added to synthesis t_aed62fd7 via comment, but synthesis v451 had already completed within <2 minutes of dispatch. The synthesis had 0 heartbeats and appeared "just spawned" — yet it finished before the comment was processed.

## Prevention — Check Status BEFORE Commenting

```python
import subprocess, json

def check_synth_status(synth_task_id):
    """Returns 'running', 'done', or 'unknown'"""
    result = subprocess.run(
        ['hermes', 'kanban', 'show', synth_task_id, '--json'],
        capture_output=True, text=True, timeout=10
    )
    try:
        d = json.loads(result.stdout)
        status = d.get('task', {}).get('status', 'unknown')
        events = d.get('task', {}).get('events', []) or []
        heartbeats = [e for e in events if e.get('kind') == 'heartbeat']
        if status == 'done':
            return 'done'
        if not heartbeats:
            return 'no_heartbeats'  # May have completed silently
        return 'running'
    except:
        return 'unknown'
```

## Key Learning

Synthesis workers complete in **<2 minutes** — far faster than the "3-8 minutes" documented in the main SKILL.md. A synthesis task with 0 heartbeats and <2 min age is NOT safe to comment on — it may already be done. The only reliable signal is `kanban show --json` reporting `status=done`.

**Revised rule:** After dispatching a synthesis task, the Director should NOT attempt to expand its scope via `kanban_comment` in the same pass. Always create a separate synthesis task for any experiments discovered after dispatch.

## Decision Tree

1. Check synthesis task status
2. If **running with recent heartbeats** (<5 min): safe to comment, worker will see it
3. If **done** or **no heartbeats for 5+ min**: synthesis completed or crashed — comment will be lost
4. In case 3: **create a separate synthesis task** for the missed experiments
5. **After dispatch:** NEVER expand a just-dispatched synthesis task — always create a separate one

## Corrected Director Workflow

When discovering unsynthesized experiments while a synthesis task is running:

```
1. Get synthesis task ID from kanban list
2. Check status: hermes kanban show <synth_id> --json
3. IF status=done OR no heartbeats in 5+ min:
     → Create NEW synthesis task for the missed experiments
4. IF status=running with recent heartbeats:
     → kanban_comment with the additional experiments
5. IF synthesis was just dispatched (same Director pass):
     → ALWAYS create NEW synthesis task (do not risk comment race)
6. NEVER skip the status check — comments to completed tasks are silently lost
```

## SUPersedes older advice (Cycle #159)

The original Cycle #159 advice said "Do NOT create a second synthesis task — use kanban_comment instead." This is WRONG for just-dispatched tasks. The comment race condition (Cycles #188, #219, #246) proved synthesis workers complete in <2 minutes, before comments are processed. **Always create a separate synthesis task for experiments discovered after dispatch.** The reference file is authoritative; the main SKILL.md's Cycle #159 section is outdated (SKILL.md at 100K char limit, cannot be patched).

## Multiple synthesis tasks for different experiment sets

When unsynthesized experiments split into groups (e.g., 8 total, 4 covered by running synthesis, 4 remaining):
- Create a SEPARATE synthesis task for the remaining group
- Do NOT try to expand the first synthesis task's scope via comment
- Both synthesis tasks can run in parallel — they write to self_state.json sequentially (the second one will see the first one's updates)
- This pattern was validated in Cycle #250: synthesis t_cbbb355d (exp_1264/1291/1296/1297) and t_daa15790 (exp_1286/1293/1299/1300) ran in parallel successfully
