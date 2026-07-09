**Pitfall — Queue items can be strings, not dicts (added Cycle #238).** The `curiosity_queue` array in `self_state.json` can contain plain strings, not just dicts with a `text` key. Code that filters with `isinstance(item, dict) and 'RESOLVED' not in item.get('text', str(item))` silently EXCLUDES string items from the active count, reporting 0 active items when there are 9+. The correct filter handles both types:
```python
# WRONG — excludes string items
active_queue = [item for item in queue if isinstance(item, dict) and 'RESOLVED' not in item.get('text', str(item))]

# CORRECT — handles both strings and dicts
active_queue = []
for item in queue:
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' not in text:
        active_queue.append(item)
```
**Detection**: If the active queue count is suspiciously low (0-2) but the total queue is 10+, suspect type mismatch. **Root cause**: Different synthesis workers may serialize queue items differently — some as `{"text": "..."}`, others as bare strings. The classification code must handle both. The code in the "Queue item classification taxonomy" section of SKILL.md already does this correctly — this pitfall catches the common mistake of writing ad-hoc filter code that assumes dict type.

**Pattern — Validate synthesis task age before kanban_comment (added Cycle #238).** Before adding experiments to a running synthesis task via `kanban_comment`, check the task's age and event count. If the synthesis task was spawned <5 minutes ago with 0 events, the worker hasn't started processing yet — safe to comment. If the task is >10 minutes old or has heartbeat events, the worker may have already read the comment thread and started processing — the comment might arrive too late. In Cycle #238, exp_1149 was added to synthesis task t_7e360a91 (3m old, 0 events) — confirmed safe. If the task were >10m old, create a separate synthesis task instead. See "Comment race condition" pitfall in SKILL.md for the full pattern.

**Pattern — Reclaim fails on done tasks with status desync (added Cycle #238).** When `kanban list --status running` shows a task but `kanban show --json` reports status=done, calling `hermes kanban reclaim` fails with "cannot reclaim (not running or unknown id)". This is correct behavior — the task is already done, not running. Do NOT retry the reclaim or assume it failed — verify via `kanban show --json` that the task is truly done (has summary/results). The desync resolves on its own as the list cache refreshes. If the done task still shows in the running list after 2+ minutes, the list cache may be stale — check `ps aux` for zombie processes (see "Zombie worker accumulation" pitfall).
