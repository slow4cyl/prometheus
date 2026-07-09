# Director Saturation Decision — Cycle 246 Worked Example

## The Problem

During Director Pass Cycle 246 (2026-06-02), the board showed 12 running tasks across 11 active worker profiles. The pre-run script reported "No running experiments" (stale data), but the live kanban board was authoritative. This created a decision point:

- 12 running tasks ≥ 7 threshold → saturation mode (synthesis-only)
- But the synthesis task for unsynthesized experiments was ALREADY dispatched
- So there was literally nothing to do this pass

## The Correct Decision Flow

```
1. Count active workers via `ps aux | grep prometheus-worker | grep -v grep`
2. Count running tasks via `hermes kanban list --status running --json`
3. Verify: each running task has a live worker process
4. If running_tasks ≥ 7:
   a. Check if synthesis task already exists for unsynthesized experiments
   b. If synthesis exists → NOTHING TO DO (log pass, chain to next cycle)
   c. If synthesis missing → CREATE synthesis task (maintenance, not investigation)
   d. NEVER create experiment tasks during saturation (even with free workers — see exception below)
5. EXCEPTION: If free_workers ≥ 3 AND 3+ genuinely OPEN queue items exist:
   a. Create tasks ONLY for items with LOW coverage (<0.15) against running tasks
   b. This is the "free workers during saturation" exception from Cycle #166
```

## Key Insight: Maintenance vs Investigation

Synthesis and curation tasks are MAINTENANCE, not investigation. The saturation threshold gates EXPERIMENT task creation, not maintenance. If you find unsynthesized experiments during a saturated pass, you CAN create a synthesis task — but you CANNOT create experiment tasks.

## What Went Right in Cycle 246

1. Correctly identified saturation (12 running ≥ 7)
2. Correctly determined synthesis task already existed (t_63c0ce85)
3. Correctly decided: nothing to do, log pass, chain to next cycle
4. Correctly identified highest-priority uncovered queue items for NEXT cycle:
   - Item [2] (score=82.0, 1-shot baseline) — LOW coverage (0.07), genuinely uncovered
   - Item [40] (NO_SOURCE, embedding modality non-English) — always valuable

## What Could Go Wrong

1. **Creating experiment tasks during saturation** → wastes worker slots, increases assessment overhead
2. **Creating duplicate synthesis tasks** → concurrent writes to self_state.json
3. **Trusting pre-run script data over live board state** → stale data leads to wrong decisions
4. **Reclaiming healthy workers** → some workers have 0 heartbeats because they run Python scripts, not hermes agents
