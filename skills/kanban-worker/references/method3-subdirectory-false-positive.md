# Method 3 False Positive: Subdirectory Workspaces

**Added:** 2026-06-02 (Cycle 245)
**Severity:** Medium — causes false STALE/DEAD diagnosis for healthy workers

## The Problem

The Method 3 workspace staleness check uses `os.listdir(ws)` + `os.path.isfile()` to find the latest modification time. This only checks **top-level files**. Workspaces that organize output into subdirectories (`data/`, `results/`, `scripts/`) will report 0 files and 999m age — falsely diagnosed as STALE or NEWLY DISPATCHED.

## Reproduction

```
# Workspace structure:
t_2301f182/
  data/           (subdirectory)
  results/        (subdirectory)
    shot_accuracy.json   ← last modified 08:31
    stopping_analysis.json
  scripts/        (subdirectory)

# Method 3 code:
files = os.listdir(ws)  # Returns: ['data', 'results', 'scripts']
# os.path.isfile() filters all out → 0 files, 999m age → "STALE"
```

## The Fix

Use `Path.rglob('*')` or `os.walk()` to find files recursively:

```python
from pathlib import Path

ws = os.path.join(workspaces, tid)
files = [f for f in Path(ws).rglob('*') if f.is_file()]
latest_mtime = max((f.stat().st_mtime for f in files), default=0)
age_min = (time.time() - latest_mtime) / 60 if latest_mtime > 0 else 999
```

## Detection

When Method 3 shows 0 files but `ls -la <workspace>` shows subdirectories, the check is hitting this pitfall. Always cross-reference with `ls` before concluding STALE.

## Real-World Example (Cycle 245)

exp_1433 workspace had `results/shot_accuracy.json` actively being written (08:31), but Method 3 reported "STALE (last mod 999m ago, 0 files)". The process was alive and producing output — the false positive nearly caused an unnecessary reclaim.
