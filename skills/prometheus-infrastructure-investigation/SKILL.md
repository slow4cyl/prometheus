---
name: prometheus-infrastructure-investigation
description: Investigate why configured Prometheus subsystems (GPU accuracy monitoring, inference health checks) are not producing expected output — trace data flow from collection to dashboard.
version: 1.0.0
platforms: [linux]
metadata:
  hermes:
    tags: [prometheus, infrastructure, debugging, gpu, monitoring, dashboard]
---

# Prometheus Infrastructure Investigation

Systematic investigation of why Prometheus monitoring subsystems fail to produce expected output. Covers GPU accuracy tracking, inference health checks, and dashboard data flow.

## When to Use

- Dashboard shows stale/missing data despite subsystem being "configured"
- GPU monitoring not producing accuracy results
- Inference health checks returning empty or stale
- Cron jobs producing no output or errors
- `self_state.json` missing expected fields despite subsystem claiming completion

## Core Principle

Most infrastructure failures follow one of three patterns:
1. **Data never collected** — the subsystem didn't run or didn't write output
2. **Data collected but not synced** — output exists but didn't reach the consumer
3. **Data synced but not surfaced** — data exists in the right place but the dashboard/query doesn't find it

Start by determining which pattern applies.

## Investigation Steps

### Step 1: Check the Source of Truth

```bash
# Check self_state.json — what does the system think happened?
python3 -c "
import json
with open('~/.hermes/self_state.json') as f:
    state = json.load(f)
print(json.dumps({k: v for k, v in state.items() if any(x in k.lower() for x in ['gpu', 'inference', 'accuracy', 'health', 'monitoring'])}, indent=2))
"

# Check if subsystem files exist
ls -la ~/.hermes/artifacts/
ls -la ~/.hermes/scripts/gpu_accuracy_monitor.py
ls -la ~/.hermes/scripts/inference_health_monitor.py
```

### Step 2: Check Execution History

```bash
# Check cron jobs for the subsystem
hermes cron list 2>/dev/null | grep -i "gpu\|inference\|accuracy\|health\|monitor"

# Check recent script output
ls -lt ~/.hermes/artifacts/gpu_accuracy* 2>/dev/null | head -5
ls -lt ~/.hermes/artifacts/inference_health* 2>/dev/null | head -5
```

### Step 3: Test the Subsystem Directly

```bash
# Try running the monitoring script manually
cd ~/.hermes
python3 scripts/gpu_accuracy_monitor.py 2>&1 | head -50

# Check for import errors or missing dependencies
python3 -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}')" 2>&1
```

### Step 4: Check Configuration

```bash
# Verify config.yaml has the subsystem enabled
grep -A5 -i "gpu\|accuracy\|inference\|monitor" ~/.hermes/config.yaml 2>/dev/null
```

### Step 5: Determine the Failure Pattern

- **Pattern 1 (never collected):** Script doesn't exist, cron job missing, or script errors on execution
- **Pattern 2 (not synced):** Script runs but output file doesn't exist or is stale
- **Pattern 3 (not surfaced):** Output exists but dashboard doesn't query it correctly

## Common Fixes

### Cross-DB reconciliation false positives

When a monitor reports completed Kanban work as missing from Prometheus, first determine the task class and its authoritative output table. Do row-based coverage (`task_id` present in the output table), not title-based suppression. If adding a coverage path, also add a separate stale/unapplied alert for that output table so the fix clears false positives without hiding real pipeline stalls.

See `references/cross-db-reconciliation-provenance.md` for the synthesis-output pattern, safe backfill rules, and the isolated `/tmp/hermes-verify-*` ad-hoc verification harness pattern.

### Pattern 1: Script Not Running
```bash
# Create the monitoring script if missing
# Example: GPU accuracy monitor
cat > ~/.hermes/scripts/gpu_accuracy_monitor.py << 'EOF'
#!/usr/bin/env python3
"""Monitor GPU inference accuracy and log results."""
import json, time, subprocess
from pathlib import Path

def check_gpu():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total',
                               '--format=csv,noheader,nounits'],
                              capture_output=True, text=True, timeout=5)
        return result.stdout.strip()
    except:
        return "unavailable"

def main():
    output = Path.home() / '.hermes' / 'artifacts' / 'gpu_accuracy_latest.json'
    output.parent.mkdir(parents=True, exist_ok=True)

    gpu_info = check_gpu()
    result = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'gpu_status': gpu_info,
        'status': 'ok'
    }

    output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))

if __name__ == '__main__':
    main()
EOF
chmod +x ~/.hermes/scripts/gpu_accuracy_monitor.py
```

### Pattern 2: Sync Issue
```bash
# Ensure output lands in the expected location
# Check where the script writes vs where the consumer reads
grep -n "output\|write\|save\|Path" ~/.hermes/scripts/gpu_accuracy_monitor.py
```

### Pattern 3: Dashboard Query
```bash
# Check what the dashboard queries
grep -r "gpu_accuracy\|inference_health" ~/.hermes/dashboard/ 2>/dev/null
```

## Diagnostic Template

```
Infrastructure Investigation: <subsystem name>

Symptoms:
- What the user sees (stale dashboard, missing data, etc.)
- When it last worked (if known)

Checklist:
- [ ] Source script exists and is executable
- [ ] Cron job configured and enabled
- [ ] Script runs without errors (manual test)
- [ ] Output file written and current
- [ ] Consumer reads the correct output path
- [ ] Dashboard queries the correct field

Root cause: <pattern 1/2/3>
Fix: <what to change>
```

## Related Skills

- `gpu-toolset` — GPU experiment management and monitoring
- `live-dashboards` — Building lightweight monitoring dashboards
- `system-health` — Hardware and inference monitoring
- `monitoring-tools` — Agent and system monitoring (three-stage health)
