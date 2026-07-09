# Pitfall: Subdirectory Output Invisible to Workspace Check

**Added:** Cycle #210
**Severity:** Medium — causes false STALE signals for healthy workers

## Problem

The Method 3 workspace staleness check uses `os.listdir(ws)` which only scans top-level entries. Experiment scripts that organize output into subdirectories (e.g., `results/`, `output/`, `data/`) produce files that are invisible to this check. The workspace appears STALE (999m) even though the worker is actively writing to subdirectory files.

## Observed In

- **Cycle #210, t_00dd7a45 (exp_671):** Workspace contained `prompts/`, `results/`, `scripts/` directories. The `os.listdir()` check saw 3 entries (directories, not files) with no top-level files. Reported STALE (999m). In reality, `results/experiment_output.txt` was being written at 4:32AM — the worker was healthy.

## Detection

When the workspace staleness check reports STALE but `ps aux` confirms the process is alive:
1. Check for subdirectory files: `find <workspace> -maxdepth 2 -type f -newer <process_start_time>`
2. Check experiment log directories: `ls -lt ~/.hermes/experiments/exp_*_output.log 2>/dev/null | head -5`
3. Check /tmp output: `ls -lt /tmp/exp_*_output.log 2>/dev/null | head -5`

## Fix

Replace `os.listdir(ws)` with `os.walk(ws)` limited to depth 2:

```python
import os, time

def workspace_latest_mtime(ws, max_depth=2):
    """Find latest file mtime across top-level and one level of subdirectories."""
    latest_mtime = 0
    latest_file = None
    for root, dirs, files in os.walk(ws):
        # Limit depth
        depth = root.replace(ws, '').count(os.sep)
        if depth >= max_depth:
            dirs.clear()  # don't recurse further
            continue
        for f in files:
            fp = os.path.join(root, f)
            mt = os.path.getmtime(fp)
            if mt > latest_mtime:
                latest_mtime = mt
                rel = os.path.relpath(fp, ws)
                latest_file = rel
    return latest_mtime, latest_file
```

## Prevention

When creating experiment task bodies, instruct workers to write output to the workspace root OR to a well-known subdirectory. The Director's workspace health check should use the depth-2 walk pattern, not flat `os.listdir()`.
