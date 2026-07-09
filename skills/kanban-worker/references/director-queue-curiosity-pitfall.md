# Director Adding Curiosities to Queue — Pitfall

**Added:** Cycle #191
**Category:** Director single-writer invariant violation

## Problem

When the Director discovers new curiosities from experiment results (e.g., from reading completed task summaries), it may be tempted to add them directly to the curiosity queue in `self_state.json`. This violates the single-writer invariant — only the synthesis worker writes to `self_state.json`.

## Example

```python
# WRONG — Director writing to self_state.json
path = os.path.expanduser('~/.hermes/self_state.json')
ss = json.load(open(path))
queue = ss.get('curiosity_queue', [])
queue.append({"text": "[NEW from exp_552] Cross-domain facts harm on DeepSeek..."})
ss['curiosity_queue'] = queue
with open(path, 'w') as f:
    json.dump(ss, f, indent=2)
```

## Fix

Instead of writing to `self_state.json`, add a `kanban_comment` to the synthesis task listing the new curiosities. The synthesis worker reads the comment thread and adds them to the queue during its synthesis pass.

```bash
# CORRECT — Director using kanban_comment
hermes kanban comment <synthesis_task_id> "NEW CURIOSITIES TO ADD TO QUEUE:
1. [NEW from exp_552] Cross-domain facts harm on DeepSeek V4 Flash
2. [NEW from exp_554] POST-injection detection via confidence calibration
3. [NEW from exp_556] Cross-domain transfer structural features
4. [NEW from exp_561] Lexical overlap AUC improvement"
```

## Prevention

When the Director identifies new curiosities during a pass, always use `kanban_comment` on the synthesis task — never write to `self_state.json` directly, even for "append-only" operations like queue additions.

## Context

This pitfall was observed in Cycle #191 where the Director:
1. Accidentally wrote 4 curiosities to the queue in `self_state.json`
2. Realized the violation of the single-writer invariant
3. Reverted the write (removed the 4 items)
4. Used `kanban_comment` to add the curiosities to the synthesis task instead

The key insight: even "append-only" operations like adding queue items must go through the synthesis worker, not direct writes to `self_state.json`.
