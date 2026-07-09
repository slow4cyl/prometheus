# OpenRouter Silent Timeout Pattern

**Added:** Cycle #226
**Category:** Systemic hung process diagnosis

## Symptom

Multiple experiment tasks (3+) simultaneously show:
- Process alive (`ps aux` confirms)
- 0% CPU time on the subprocess after 20+ minutes
- 0 heartbeats, 0 completions
- Workspace files unchanged since initial creation

When 3+ tasks exhibit this pattern simultaneously, the root cause is almost always an **API provider outage or rate limit**, not individual task problems.

## Root Cause

OpenRouter API calls can timeout silently — the HTTP connection hangs without raising an exception or returning an error. The Python subprocess stays alive (waiting on the HTTP response) but does zero work. This is distinct from:
- **Crashed process**: process disappears from `ps aux`
- **Slow computation**: CPU time accumulates steadily
- **Healthy long API call**: CPU time jumps in bursts (60-120s per call)

## Detection

```bash
# Step 1: Find all experiment subprocesses
ps aux | grep 'python3.*exp_' | grep -v grep

# Step 2: Check CPU time for each
# If 3+ show <1s CPU after 20+ min → systemic issue

# Step 3: Verify it's OpenRouter-specific
# Check if tasks calling local vLLM are affected
# If only OpenRouter tasks are hung → provider issue
```

## Recovery

1. **Reclaim** all affected tasks (releases worker claims)
2. **Block** all affected tasks with reason noting the systemic cause
3. **Kill** zombie subprocesses
4. **Log** the systemic event for pattern tracking
5. **Do NOT re-dispatch** until the API provider is confirmed healthy

## Prevention

Experiment scripts should implement:
- Explicit HTTP timeout (30-60s max per call)
- Retry with exponential backoff (3 attempts)
- Partial result saving (write intermediate results before each API call)
- Health check before starting (test a simple API call first)

Example timeout wrapper:
```python
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))
session.mount('http://', HTTPAdapter(max_retries=retries))

response = session.post(url, json=payload, timeout=60)
```

## Relation to Existing Patterns

This extends the "hung task lifecycle" pitfall (Cycle #190) by identifying a specific **systemic cause**. The existing reclaim→kill→block pattern is correct — this file explains WHEN to apply it in bulk (3+ simultaneous hangs = systemic, not individual).

Also extends the "subprocess CPU time diagnostic" (Cycle #218) by noting that when multiple subprocesses show 0 CPU simultaneously, the issue is upstream (API provider), not per-task.
