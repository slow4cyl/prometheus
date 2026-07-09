# Multi-Signal Workspace Staleness Check

Combined technique for diagnosing worker health using multiple signals. More reliable than any single check.

## Signals

### Signal 1: Process Existence (`ps aux`)
Ground truth for whether the worker process is alive.

```bash
ps aux | grep <task_id> | grep -v grep
```

**Interpretation**:
- Process found with CPU time > 0: Worker is alive
- Process not found: Worker crashed
- Process found with 0 CPU time: May be hung (check other signals)

### Signal 2: Workspace File Modification Time
Check if the worker is actively writing files.

```python
import os, time

ws = os.path.expanduser(f'~/.hermes/kanban/workspaces/{task_id}')
files = os.listdir(ws)
latest_mtime = max(os.path.getmtime(os.path.join(ws, f)) 
                   for f in files if os.path.isfile(os.path.join(ws, f)))
age_min = (time.time() - latest_mtime) / 60

status = 'ACTIVE' if age_min < 5 else ('SLOW' if age_min < 15 else 'STALE')
```

**Interpretation**:
- ACTIVE (<5m): Worker is writing files
- SLOW (5-15m): Worker may be in API call
- STALE (>15m): Check other signals

### Signal 3: Output Log Files
Many scripts write to `~/.hermes/experiments/` or workspace.

```python
# Check workspace for output logs
for f in os.listdir(ws):
    if 'output' in f and f.endswith('.log'):
        path = os.path.join(ws, f)
        age_min = (time.time() - os.path.getmtime(path)) / 60
        print(f"Output log: {age_min:.0f}m ago")

# Also check external locations
for logdir in ['~/.hermes/experiments', '/tmp']:
    for f in os.listdir(os.path.expanduser(logdir)):
        if task_id in f or exp_id in f:
            # Found output log
```

### Signal 4: Script Content Analysis
Understand what the experiment is doing.

```python
script = os.path.join(ws, f'{exp_id}.py')
with open(script) as f:
    content = f.read()

# Check for API calls
uses_openrouter = 'OpenRouter' in content
uses_local_vllm = 'localhost' in content or 'vllm' in content

# Check for sleep calls
import re
sleep_calls = re.findall(r'time\.sleep\((\d+)\)', content)

# Check for domains
domains = re.findall(r'(DICT|PLATFORM|DISPATCH|METHODOLOGY|REGULATORY)', content)
```

## Combined Diagnosis Matrix

| Process | Workspace | Output Log | Diagnosis | Action |
|---------|-----------|------------|-----------|--------|
| Alive | ACTIVE | Present | Healthy | Leave alone |
| Alive | SLOW | Present | Long API call | Leave alone |
| Alive | STALE | Present | In API call | Check CPU time |
| Alive | STALE | Absent | Potentially hung | Monitor, block if >60m |
| Alive | ACTIVE | Absent | Writing to external | Check /tmp, experiments/ |
| Dead | Any | Any | Crashed | Block immediately |

## CPU Time Diagnostic

For processes that are alive but may be hung:

```bash
ps aux | grep <task_id> | grep -v grep
# Check TIME column (cumulative CPU seconds)
```

**Interpretation**:
- CPU time accumulating steadily: Healthy (doing API calls)
- CPU time frozen at low value: Hung (stuck on API call or I/O)

**Example**:
- Healthy: `0:30.45` after 30 minutes (accumulating)
- Hung: `0:01.46` after 37 minutes (frozen)

## Real-World Examples

### Example 1: Healthy Long-Running Experiment (exp_695)
- Process: Alive, CPU=10.59s
- Workspace: 2 files, latest 0m ago
- Output log: Present, updated 0m ago
- **Diagnosis**: ACTIVE — experiment is running and producing output

### Example 2: Potentially Hung Experiment (exp_701)
- Process: Alive, CPU=22.17s
- Workspace: 1 file (script only), 25m stale
- Output log: Absent
- **Diagnosis**: STALE — process alive but no output. Could be:
  - Still in setup phase (unlikely after 42 minutes)
  - Stuck on API call
  - Running but not producing output yet
- **Action**: Monitor. If still stale at 60m, block with reason.

### Example 3: Status Desync (exp_702)
- Process: Found in ps aux (shell wrapper)
- Kanban list: Shows "running"
- Kanban show: Shows "done" with summary
- **Diagnosis**: Status desync — task completed but list view not updated
- **Action**: Ignore. Task is done. Process may be zombie.

## Pitfall: Output Logs Outside Workspace

Scripts often write to `~/.hermes/experiments/` or `/tmp/` instead of workspace.

**Detection**: When workspace shows STALE but process is alive, check:
```bash
ls -lt ~/.hermes/experiments/exp_*_output.log 2>/dev/null | head -5
ls -lt /tmp/exp_*_output.log 2>/dev/null | head -5
```

**Fix**: The most recent log file's modification time is the true last-activity signal.

## Pitfall: "Just Dispatched" False Positive

Tasks dispatched in the current cycle show 0 files and 999m age. This is NORMAL — the worker hasn't started writing files yet.

**Detection**: Cross-reference with `ps aux`. If process exists and task is <10 minutes old, leave alone.
