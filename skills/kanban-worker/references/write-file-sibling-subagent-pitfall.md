# write_file Sibling Subagent Interception Pitfall

**Added:** Cycle #255
**Context:** Director pass cron job running concurrently with other agent processes

## Problem

When multiple Hermes agent processes run concurrently (e.g., cron job + interactive session + worker processes), the `write_file` tool can be intercepted by a sibling subagent. The tool returns:

```
"_warning": "/tmp/script.py was modified by sibling subagent '82b99937-...' but this agent never read it."
```

This causes the write to succeed but with unexpected content (the sibling's version), or the file gets overwritten before the current agent can read it.

## Detection

- `write_file` returns a `_warning` field about sibling subagent modification
- Scripts written to `/tmp/` get modified between write and execution
- Unexpected content when reading back files that were just written

## Impact

- Scripts execute with wrong content (sibling's version)
- Multiple retries needed to get correct file content
- Significant time waste in Director passes (10+ intercepts observed)

## Workaround

1. **Read before write:** Always `read_file` before `write_file` if the file already exists
2. **Use unique filenames:** Add timestamp or PID to filenames: `/tmp/script_${PID}.py`
3. **Write directly to workspace:** Use `$HERMES_KANBAN_WORKSPACE` instead of `/tmp/`
4. **Check content after write:** Always verify the file content matches expectations before executing

## Example

```python
# BAD — may be intercepted
write_file(path="/tmp/script.py", content="...")
terminal(command="python3 /tmp/script.py")

# GOOD — read first, verify content
read_file(path="/tmp/script.py")  # Check if it exists and what's in it
write_file(path=f"/tmp/script_{os.getpid()}.py", content="...")
# Verify before execute
content = read_file(path=f"/tmp/script_{os.getpid()}.py")
if expected_content in content:
    terminal(command=f"python3 /tmp/script_{os.getpid()}.py")
```

## Why This Happens

Hermes Agent allows multiple concurrent sessions (cron jobs, workers, interactive). When two sessions target the same file path, the second write overwrites the first. The `_warning` is a safety check, but it doesn't prevent the overwrite — it just alerts.

## Prevention

- Use PID-based or timestamp-based unique filenames for temporary scripts
- Write to workspace directories instead of shared `/tmp/`
- For Director passes, consider writing scripts to `~/.hermes/scripts/` with unique names
