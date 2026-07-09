# Worker Over-Blocking on Infrastructure Notes in Task Bodies (Cycle #229)

## Problem

When a Director creates experiment tasks with infrastructure routing notes, workers may interpret these notes as hard requirements and block immediately when the referenced infrastructure is unreachable — even when the experiment itself doesn't require that specific model.

## Example (Cycle #229)

Director created 5 experiment tasks, all including the standard infrastructure note referencing a Qwen model endpoint. The endpoint was unreachable. Results:
exp_1012 (attention fusion): BLOCKED — needs Qwen attention patterns. Legitimate block.
exp_1013 (dialogue FPR): BLOCKED — "requires Qwen3.6-35B-A3B". INCORRECT — experiment is about belief-shift detection, works fine with mimo-v2.5.
exp_1014 (GDPR terminology): BLOCKED — worker checked endpoint connectivity and blocked. INCORRECT — experiment is about context-enhancement strategies, works with any model.
exp_1016 (calibration comparison): BLOCKED — needs Qwen confidence scores. Partially legitimate.

3 out of 4 blocked tasks could have run with alternative models (mimo-v2.5, deepseek-v4-flash via OpenRouter).

## Root Cause

Workers read the infrastructure note as "this experiment REQUIRES Qwen" rather than "IF you use Qwen, here's how to route it." The note's phrasing ("MUST use") is interpreted as a task requirement, not a routing preference.

## Impact

- Wasted dispatch cycles (create → dispatch → block → unblock → re-dispatch)
- Workers sit idle when they could be productive
- Director must manually unblock and re-dispatch

## Prevention

When creating experiment tasks, distinguish between:
1. **Experiments that REQUIRE a specific model** (e.g., attention pattern extraction needs Qwen's architecture): State explicitly "THIS EXPERIMENT REQUIRES Qwen3.6-35B-A3B. If the Qwen endpoint is unreachable, BLOCK immediately."
2. **Experiments that CAN USE multiple models** (e.g., accuracy comparison, detection evaluation): State "Use mimo-v2.5 and deepseek-v4-flash via OpenRouter."

Alternative: Include a "MODEL REQUIREMENTS" section in the task body that explicitly states which models are required vs optional.

## Fix (when already blocked)

If a worker blocks on infrastructure that isn't actually required:
1. `hermes kanban unblock <task_id> "Endpoint down but experiment can run with mimo/deepseek. Use OpenRouter."`
2. `hermes kanban dispatch` to re-spawn
3. The worker will retry with alternative models

Note: `hermes kanban unblock` shows a misleading error message but DOES succeed (see existing pitfall "hermes kanban unblock shows misleading error but succeeds").
