# Skip Threshold and Maintenance Tasks — Clarity Update (Cycle from 2026-06-01)

## The Rule

The pre-run skip threshold gates the **INVESTIGATION cycle** (Steps 3-8 of director-loop), NOT maintenance. Maintenance tasks are always allowed, even when the skip threshold fires.

## What Counts as Maintenance (always allowed)
- Synthesis tasks (consolidating unsynthesized experiments)
- Queue curation (when queue > 50 items)
- Duplicate detection and reclamation
- Worker liveness checks
- Counter reconciliation

## What Counts as Investigation (gated by skip threshold)
- Creating new experiment tasks
- Full queue triage for task creation
- Dependency mapping for new experiments

## Director Behavior on Skip Tick
1. Check board for unsynthesized experiments → create synthesis task if needed
2. Check queue size → create curation task if > 50 items
3. Check for duplicates → reclaim + block
4. Do NOT create new experiment tasks
5. Log as `DIRECTOR PASS` with note: "investigation skipped, maintenance dispatched"

## Skip Threshold Negative Value Pitfall

The skip message may display negative values (e.g., `-366s ago`) when self_state.json was updated recently. The comparison logic should treat the absolute value: `abs(age) < threshold` means skip. A negative age simply means "updated N seconds ago."

Example from 2026-06-01:
```
SKIP: self_state.json updated -366s ago (< 90s threshold)
```
This means self_state.json was updated 366 seconds ago. Since 366 > 90, this should NOT trigger a skip. The negative sign is a display artifact from the age calculation (current_time - file_mtime can go negative if the file was modified after the script started).

The correct comparison: if `abs(age) < 90`, skip investigation. If `abs(age) >= 90`, proceed with investigation.
