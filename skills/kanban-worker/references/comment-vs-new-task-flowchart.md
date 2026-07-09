# Comment vs. New Task Decision Flowchart

Added Cycle #201 — concrete decision framework for the synthesis task race condition.

## The Problem

When the Director discovers unsynthesized experiments and a synthesis task is already running, two options exist:
1. **Comment** on the running synthesis task: `kanban_comment(synthesis_task_id, "ADDITIONAL: Also synthesize exp_XXX...")`
2. **Create** a new synthesis task for the missed experiments

Option 1 is faster but risks the race condition: the synthesis task completes before the comment is processed, and the missed experiments stay unsynthesized.

## Decision Flowchart

```
1. CHECK SYNTHESIS TASK STATUS
   hermes kanban show <synthesis_task_id> --json > /tmp/synth_status.json
   
2. PARSE STATUS (separate command — TIRITH blocks pipes)
   python3 -c "import json; d=json.load(open('/tmp/synth_status.json')); print(d.get('task',{}).get('status','?'))"

3. DECIDE:
   IF status == "running":
     CHECK heartbeat recency (Method 2 from liveness section)
     IF last heartbeat < 2 min ago:
       → COMMENT is safe (worker is alive and will read thread)
       → kanban_comment(synthesis_task_id, "ADDITIONAL: Also synthesize exp_XXX...")
     IF last heartbeat 2-10 min ago:
       → COMMENT is RISKY (worker may be mid-write or about to complete)
       → PREFER: create new synthesis task (safer, no race)
     IF last heartbeat > 10 min ago OR no heartbeats:
       → COMMENT will likely be missed (worker may be stuck or completing)
       → CREATE new synthesis task
   IF status == "done":
     → COMMENT is too late (task already completed)
     → CREATE new synthesis task for missed experiments
   IF status == "blocked":
     → Task is blocked, won't process comments
     → CREATE new synthesis task
```

## Why This Matters

The race condition (comment arrives after synthesis completes) wastes a synthesis cycle — the missed experiments stay unsynthesized until the next Director pass discovers them again. The cost is 5-10 minutes of delay per occurrence. Creating a new task is always safe; commenting is only safe when the worker is actively processing.

## Real-World Example (Cycle #201)

**Situation:** Added exp_617/619/620/622 to running synthesis task t_41da2f32 via kanban_comment. Task was 4 minutes old with no heartbeats.

**What happened:** Task completed before comment was processed — synthesis worker only saw the original 5 experiments (exp_608-611, exp_626), missed the 4 added via comment.

**Remedy:** Created new synthesis task t_2126af28 for the 6 missed experiments (exp_617, exp_619, exp_620, exp_622, exp_630, exp_633).

**What should have happened:** Per the flowchart, a task with no heartbeats at 4 minutes old should have triggered "CREATE new synthesis task" instead of commenting.

## Scope Mismatch Criterion (added Cycle #255)

Even when the 0-events check says commenting is safe, consider whether the new experiments **fit the original synthesis task's scope**:

- **Same scope** (e.g., synthesis covers exp_1574-1580, new experiments are exp_1581-1583 from same research thread): Comment is clean — the synthesis worker processes a coherent batch.
- **Different scope** (e.g., synthesis covers exp_1574-1580 (embedding gating), new experiments are exp_1581, 1584, 1585, 1592 (calibration, C-extension, N-shot — different threads)): Separate task is cleaner — avoids mixing unrelated findings in one synthesis summary.

**Why scope matters:** A synthesis task titled "consolidate exp_1574, 1575, 1578, 1579, 1580" that also processes exp_1581 (character calibration), exp_1584 (C-extension), exp_1585 (embedding modality), and exp_1592 (N-shot prediction) produces a muddled summary covering 4 unrelated research threads. Separate synthesis tasks produce focused, actionable summaries.

**Rule:** Use the flowchart's timing check FIRST. If timing says "comment is safe" AND the experiments are from the same research thread → comment. If timing says "safe" BUT experiments are from different threads → still create a separate task.

## Cost Analysis

| Action | Time Cost | Risk |
|--------|-----------|------|
| Comment (safe window, same scope) | 0 extra | Low — worker reads thread |
| Comment (safe window, different scope) | 0 extra | Low timing risk, but muddled summary |
| Comment (risky window) | 5-10 min delay if missed | Medium — need new task anyway |
| Create new task | 0 extra | None — always works, clean summaries |

**Recommendation:** When in doubt, create a new task. The overhead is zero (same worker, same API cost), and it eliminates both the race condition and scope-mixing problems.
