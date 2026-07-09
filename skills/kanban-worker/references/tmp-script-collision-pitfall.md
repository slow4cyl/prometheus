# `/tmp` Script Collision from Sibling Subagents (added Cycle #220)

## Symptom

When the Director writes analysis scripts to `/tmp/check_board.py`, `/tmp/analyze_board.py`, etc., sibling subagents (other workers running concurrently) write to the SAME paths. The `write_file` tool emits warnings:

```
Warning: /private/tmp/check_board.py was modified by sibling subagent '8bea6f8a...' 
but this agent never read it.
```

This causes:
1. **Lost analysis**: Your script gets overwritten before you run it
2. **Wrong results**: You run a script written by a different agent with different logic
3. **Silent data corruption**: The script runs successfully but produces another agent's output

## Root Cause

`/tmp` is a shared namespace. All Hermes agents (Director, workers, synthesis) share the same `/tmp`. The `write_file` tool doesn't lock files — any agent can overwrite any `/tmp` file at any time.

## Fix — Use Unique Prefixes

```python
# WRONG — generic names collide with sibling agents
write_file(path='/tmp/check_board.py', content='...')
write_file(path='/tmp/analyze_board.py', content='...')

# CORRECT — use task ID or timestamp prefix
import os, time
prefix = f'/tmp/dir_{os.getpid()}_{int(time.time())}'
write_file(path=f'{prefix}_check_board.py', content='...')
write_file(path=f'{prefix}_analyze_board.py', content='...')
```

Or use the task ID from the environment:
```python
task_id = os.environ.get('HERMES_KANBAN_TASK', 'director')
prefix = f'/tmp/{task_id}'
write_file(path=f'{prefix}_check.py', content='...')
```

## When This Matters Most

- **Director passes** that create multiple analysis scripts in a single cycle
- **Parallel workers** that share the same `/tmp` namespace
- **Cron jobs** running concurrently with manual sessions

## Workaround When Collision Happens

If you detect the warning, re-read the file before running it:
```python
# Read the file you just wrote to verify it's still yours
content = read_file(path='/tmp/check_board.py')
# If content doesn't match what you wrote, re-write it
```

Or skip the intermediate script entirely — use `terminal` with inline Python:
```bash
python3 -c "import json; d=json.load(open('/tmp/kanban_running.json')); print(len(d))"
```
This avoids `/tmp` file collision because there's no file to overwrite.
