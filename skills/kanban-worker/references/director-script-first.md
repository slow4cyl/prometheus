# Director: USE EXISTING SCRIPTS FIRST

Before hand-coding any Director logic, check if a reusable script already covers it. The kanban-worker skill ships two scripts under `scripts/`:

- **`scripts/director_queue_triage.py`** — Full queue classification + coverage scoring + unsynthesis detection + zombie count in one command. Reads `/tmp/kanban_running.json` and `/tmp/kanban_done.json`.
- **`scripts/director_health_check.py`** — Bulk workspace liveness check (ACTIVE/SLOW/STALE) for all running tasks. Reads `/tmp/kanban_running.json`.

## Usage pattern

```bash
# Step 1: Dump board state (TIRITH-safe two-step)
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json

# Step 2: Run the script (don't hand-code what's already scripted)
python3 ~/.hermes/skills/devops/kanban-worker/scripts/director_queue_triage.py
python3 ~/.hermes/skills/devops/kanban-worker/scripts/director_health_check.py
```

## What director_queue_triage.py covers

- Extracts experiment IDs from done task titles (excluding synthesis tasks)
- Compares against self_state.json to find unsynthesized experiments
- Classifies queue items: RESOLVED / RUNNING / DONE / NO_SOURCE / UNKNOWN
- Computes keyword-overlap coverage scores (HIGH/MED/LOW) against running task titles
- Counts zombies by comparing worker processes against running task IDs
- Outputs actionable recommendations (which NO_SOURCE items to create tasks for)

## What director_health_check.py covers

- Checks workspace modification time for every running task
- Reports ACTIVE (<5m) / SLOW (5-15m) / STALE (>15m) status
- Shows file count per workspace
- Outputs summary table

## Lesson from Cycle #201

The Director wrote 5 custom Python scripts that duplicated these existing scripts:
- `director_check.py` → duplicated zombie detection from triage script
- `director_state_check.py` → duplicated queue classification from triage script
- `director_coverage.py` → duplicated coverage scoring from triage script
- `director_workspace_check.py` → duplicated health check script
- `director_stuck_check.py` → duplicated heartbeat check

All of this was already in `director_queue_triage.py` and `director_health_check.py`. The skill mentions these scripts but not prominently enough. **Always run the existing scripts first, then only hand-code what they don't cover.**

## Fixes applied (2026-06-01)

- **`director_queue_triage.py`**: Fixed f-string backslash syntax errors in `extract_exp_ids()` and sorted-ID output (Python ≤3.11 limitation). Replaced `f'exp_{m.group(1)}'` with `'exp_%s' % m.group(1)`. See `references/f-string-backslash-pitfall.md` for details.
- **`director_queue_triage.py`**: Added duplicate experiment ID detection — reports when the same experiment appears in multiple running task titles, which wastes workers.
