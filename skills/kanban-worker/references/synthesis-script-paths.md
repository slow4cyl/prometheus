# generate_synthesis_body.py Path Resolution — June 4 2026

## Problem

When calling `generate_synthesis_body.py` from a Python script via `subprocess.run`, the path must be exact. The common mistake is using `os.path.expanduser('~') + '/scripts/...'` which resolves to `~/scripts/` — the script is actually at `~/.hermes/scripts/generate_synthesis_body.py`.

## Error Pattern

```python
# WRONG — resolves to ~/scripts/ (doesn't exist)
result = subprocess.run(
    ['python3', f'{HOME}/scripts/generate_synthesis_body.py', '--experiments', exp_str],
    capture_output=True, text=True, timeout=30
)
body = result.stdout.strip()  # empty string!

# RIGHT — resolves to ~/.hermes/scripts/
result = subprocess.run(
    ['python3', f'{HOME}/.hermes/scripts/generate_synthesis_body.py', '--experiments', exp_str],
    capture_output=True, text=True, timeout=30
)
body = result.stdout.strip()  # proper synthesis body
```

## Impact

- Script runs silently (no error, just empty stdout)
- Synthesis tasks get minimal fallback bodies (~200 chars) instead of proper WHY IT WORKS + CROSS-POLLINATION templates
- Workers receive incomplete instructions, may produce low-quality synthesis

## Detection

Check synthesis task body length. If < 200 chars, this path bug is likely the cause.

## Related Scripts

All synthesis-related scripts live in `~/.hermes/scripts/`:
- `generate_synthesis_body.py` — generates task body with WHY IT WORKS + CROSS-POLLINATION
- `write_synthesis_output.py` — writes to synthesis_outputs table
- `write_worker_result.py` — writes experiment results
- `safe_kanban_create.py` — dedup-gated task creation
- `queue_curator.py` — stale queue item removal
- `batch_create_tasks.py` — batch task creation with dedup
