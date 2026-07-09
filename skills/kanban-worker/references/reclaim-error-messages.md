# Reclaim Error Messages — Complete Reference

Added: 2026-06-02 (Director pass observation)

## Error: "cannot reclaim t_xxx (not running or unknown id)"

**When it fires:** `hermes kanban reclaim` is called on a task whose actual status is `done` (not `running`).

**Root cause:** Status desync between `kanban list --status running` (shows the task as running) and `kanban show --json` (reports actual status=done). The task completed via `kanban_complete` from inside a worker, but the list view hasn't refreshed.

**Why reclaim fails:** `reclaim` only works on tasks with `status=running`. A done task has no active worker claim to release.

**Diagnosis:**
```bash
hermes kanban show t_xxx --json 2>&1 > /tmp/task_check.json
python3.11 -c "import json; d=json.load(open('/tmp/task_check.json')); print(d.get('task',{}).get('status','?'))"
```

**Resolution depends on context:**
- **Task completed normally** (has summary, has events): Leave it alone. No action needed.
- **Task completed but a NEW worker was dispatched** (zombie duplicate): Block with reason noting the duplicate: `hermes kanban block t_xxx "duplicate of completed exp_YYY — already done"`
- **Task shows done but has NO summary and NO events** (phantom done): Likely a CLI bug. Leave it — the task is effectively dead.

## Error: "cannot block [reason] (not blocked/scheduled?)"

**When it fires:** `hermes kanban block` is called on a task that was blocked by the agent (not the operator), or on a task that is already done.

**Behavior:** The error message is misleading — the task IS actually unblocked/promoted. This is a cosmetic CLI bug.

**Verification:** Check `hermes kanban list --status ready` or `--status running` after the unblock. Do NOT retry.

## Contrast with reclaim on blocked tasks

`hermes kanban reclaim` on a `blocked` task **silently does nothing** — no error, no status change. The task stays blocked. This is different from the "cannot reclaim" error on done tasks. For blocked tasks, use `hermes kanban unblock <task_id> --reason "..."` instead.

## Summary Table

| Task Status | `reclaim` result | `block` result | Correct action |
|-------------|-----------------|----------------|----------------|
| `running` | ✅ Releases claim | ✅ Blocks | reclaim to release, block to prevent re-dispatch |
| `blocked` | Silent no-op | Already blocked | unblock to release |
| `done` | "cannot reclaim" error | ✅ Blocks | Leave alone (or block if zombie duplicate) |
| `ready` | "cannot reclaim" error | ✅ Blocks | reclaim does nothing useful |
