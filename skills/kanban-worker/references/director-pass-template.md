# Director Pass Report Template

Standardized format for Director pass output. Use this structure in the audit log and final report.

## Template

```
**DIRECTOR PASS #N COMPLETE**

## Board State Summary

| Status | Count |
|--------|-------|
| Running | X tasks (N active experiments + M done/desync + K synthesis) |
| Done | Y tasks |
| Ready | Z tasks |
| Workers | A/22 busy |

## Actions Taken

### 1. Board Analysis
- Synthesis status (up-to-date / unsynthesized count)
- Status desync detected (list which tasks)

### 2. Stuck Task Assessment
- For each task >20m old: status (ACTIVE/STALE/DONE), evidence

### 3. Tasks Created
Table format:
| ID | Experiment | Worker | Queue Item |
|----|-----------|--------|------------|

### 4. Queue Coverage
- Total items
- Items now covered by new/running experiments
- Items remaining unassigned

## Next Steps
- Monitor priorities
- Follow-up actions needed
```

## Example (from Cycle #212)

```
**DIRECTOR PASS #212 COMPLETE**

## Board State Summary

| Status | Count |
|--------|-------|
| Running | 11 tasks (8 active experiments + 2 done/desync + 1 synthesis) |
| Done | 818 tasks |
| Ready | 0 tasks |
| Workers | 22/22 busy |

## Actions Taken

### 1. Board Analysis
- Synthesis up-to-date: All 585 done experiments in self_state.json (650 total)
- Status desync: exp_702 and synthesis task show "running" but are "done"

### 2. Stuck Task Assessment
- exp_695 (46m): ACTIVE — output log updated 0m ago
- exp_700 (46m): ACTIVE — output log updated 3m ago
- exp_701 (46m): STALE — no output log, process alive
- exp_702 (46m): DONE — already completed

### 3. Tasks Created (13 experiments)
| ID | Experiment | Worker | Queue Item |
|----|-----------|--------|------------|
| exp_718 | Anti-sycophancy framing generalization | 7 | #34 |
| exp_719 | PLATFORM ceiling larger ensemble | 8 | #35 |
...

### 4. Queue Coverage
- 42 items total
- 2 NO_SOURCE covered (exp_718, 719)
- 13 DONE items covered by new tasks
- 5 items covered by running experiments
- 22 items unassigned

## Next Steps
- Monitor experiment progress
- Check exp_701 if remains stale
- Synthesize when experiments complete
```

## Audit Log Entry Format

```python
entry = f"""
=== DIRECTOR PASS ===
Timestamp: {timestamp}
Cycle: #{cycle_number} (autonomous)

BOARD STATE:
- Running: {running_count} tasks ({active_experiments} experiments + {done_desync} done + {synthesis_count} synthesis)
- Done: {done_count} tasks
- Ready: {ready_count} tasks
- Free workers: {free_count}

SYNTHESIS STATUS:
- {synthesis_summary}

STUCK TASK CHECK:
- {stuck_tasks}

TASKS CREATED: {created_count}
{task_list}

DISPATCH: {dispatch_result}

QUEUE STATUS:
- {queue_summary}

NEXT CYCLE: {next_actions}
"""
```
