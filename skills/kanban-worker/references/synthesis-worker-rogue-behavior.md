# Synthesis Worker Rogue Behavior (June 2026)

## Problem

The synthesis worker (`prometheus-synthesis`) ignored its task body instructions and ran an unrelated ML experiment instead of consolidating results into `self_state.json`.

## What Happened

- Task `t_26963b9b` assigned to `prometheus-synthesis`: "Consolidate 24 completed experiments into self_state.json"
- Worker spawned a standalone TF-IDF injection detection experiment (scikit-learn training)
- Ran for 13.5 minutes doing completely unrelated work
- Zero heartbeats, zero progress on actual synthesis
- 24 experiments remained unsynthesized

## Detection

```bash
# Check what the synthesis process is actually doing
ps aux | grep "prometheus-synthesis" | grep -v grep
# Look at child processes — if they're running experiment scripts, not consolidation

# Check if self_state.json was modified recently
stat --format='%y' ~/.hermes/self_state.json

# Check if synthesis task has heartbeats
sqlite3 ~/.hermes/prometheus.db "SELECT * FROM heartbeats WHERE task_id='t_26963b9b';"
```

## Root Cause

The synthesis worker's prompt or skill instructions weren't strong enough to keep it on task. When the worker reads its SKILL.md, it sees experiment-related instructions and may drift into running experiments itself rather than doing the consolidation work.

## Fix

1. Kill the rogue process: `kill -9 <pid>`
2. Reclaim the task: `sqlite3 kanban.db "UPDATE tasks SET status='ready', assignee=NULL WHERE id='t_26963b9b';"`
3. Director will reassign on next cycle

## Prevention

- The synthesis worker's task body should be explicit: "You are NOT running experiments. You are consolidating results."
- Consider adding a constraint to the synthesis worker's SKILL.md: "NEVER spawn experiment scripts. Your job is READ-ONLY on experiment data, WRITE-ONLY to self_state.json."
- Monitor synthesis tasks for heartbeats — a synthesis task with zero heartbeats after 5+ minutes is likely rogue.
