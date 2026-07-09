# `latest_summary` Null Pitfall (added Cycle #220)

## Symptom

```python
d = json.load(open('/tmp/task_summary.json'))
summary = d.get('latest_summary', 'N/A')[:200]  # ← TypeError if latest_summary is None
# TypeError: 'NoneType' object is not subscriptable
```

## Cause

`kanban show --json` returns `"latest_summary": null` (not `"N/A"`, not missing) when a task has no completion summary. This happens for:
- Newly spawned tasks that haven't completed yet
- Tasks that completed via `kanban_complete` without a summary field
- Tasks where the summary hasn't been written yet

This is the same pattern as `comments: null` (documented in the main SKILL.md) — both fields use JSON `null` instead of empty string/array.

## Fix

```python
# WRONG — crashes on null
summary = d.get('latest_summary', 'N/A')[:200]

# CORRECT — handles null
summary = (d.get('latest_summary') or 'N/A')[:200]
```

Or more defensively:
```python
summary = d.get('latest_summary')
if summary:
    summary = summary[:200]
else:
    summary = 'N/A'
```

## When This Matters

This pitfall is most common in Director passes that batch-read task summaries. The Director script calls `kanban show` for 5-10 tasks in a loop, and a single null `latest_summary` crashes the entire batch — losing all previously-read summaries.

## See Also
- `comments: null` pitfall (main SKILL.md) — same JSON null pattern
- `kanban show --json` nested structure (main SKILL.md) — navigation guide
