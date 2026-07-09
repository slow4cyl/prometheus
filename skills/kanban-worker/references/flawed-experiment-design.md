# Flawed Experiment Design — Alive but Unworkable

Added: Cycle #269 (exp_1433)

## The Pattern

A process can be alive, producing output, and writing to the workspace — yet the experiment is fundamentally unworkable because it's designed to do too many API calls in a single run. This is distinct from:
- **"Alive but silent"**: Process alive but zero output files (hung on I/O or API)
- **"Truly stuck"**: Process dead or zero CPU time

## Diagnostic

Check completion rate vs expected runtime:
1. Process alive via `ps aux`
2. Workspace has output files (not silent)
3. `progress.json` or log shows <5% completion
4. Runtime already exceeds 2x the estimated duration

## Example (exp_1433)

- **Experiment**: N-shot optimal stopping — 10 domains × 50 shots × 5 test examples = 2,500 API calls
- **Script estimate**: ~30 minutes
- **Actual after 66 min**: 21/2500 shots completed (0.84%)
- **Actual throughput**: ~3 shots/minute
- **Projected total**: ~14 hours (28x the estimate)

## Detection Pattern

```python
import json, os, time

progress_path = os.path.join(workspace, 'results', 'progress.json')
if os.path.exists(progress_path):
    progress = json.load(open(progress_path))
    completed = sum(len(v) if isinstance(v, list) else v for v in progress.get('completed_domains', {}).values())
    total_expected = 2500  # from experiment design
    pct = completed / total_expected * 100
    age_min = (time.time() - os.path.getmtime(progress_path)) / 60
    if pct < 5 and age_min > 60:
        print(f"FLAWED DESIGN: {pct:.1f}% complete after {age_min:.0f}m")
```

## Action

1. **Reclaim** the task: `hermes kanban reclaim <task_id>`
2. **Kill** the stuck process: `kill <PID>`
3. **Block** with design flaw reason: `hermes kanban block <task_id> "hung: experiment design needs chunking — 2500 API calls exceeds single-run budget"`
4. **Do NOT re-dispatch** without redesigning to chunk the work (e.g., 10 smaller runs of 250 calls each)

## Contrast with Other Hung Patterns

| Pattern | Process | Output | Completion | Root Cause |
|---------|---------|--------|------------|------------|
| Flawed design | Alive | Files being written | <5% after 2x estimate | Too many API calls per run |
| Alive but silent | Alive | Zero files | 0% | Hung on I/O or API call |
| Truly stuck | Dead | May have stale files | 0% | Crash or OOM |
| Long computation | Alive | Files being written | Progressing normally | Just takes time |
