# Director Quick-Pass Flowchart — Updated Cycle #228

This replaces the inline flowchart in SKILL.md. The key additions are:
- Step 1 now dumps blocked tasks too
- New Step 3: CHECK BLOCKED TASKS AND INFRASTRUCTURE
- Step 4 now handles <3 unsynthesized by adding to running synthesis task
- Step 5 excludes items blocked by infrastructure
- Step 7 excludes topics blocked by infrastructure

## Updated Flowchart

```
0. CHECK SKIP THRESHOLD (pre-run script output)
   IF pre-run script says "SKIP: self_state.json updated < 90s ago":
     → Investigation gated. Skip steps 6-7 (no new experiment tasks).
     → Still execute: steps 1-5, 8-9 (board check, synthesis, audit).
     → See references/director-skip-threshold-decision.md for full logic.
   IF pre-run script says "OK" or no skip message:
     → Full workflow (all steps).

1. DUMP BOARD STATE (two-step, TIRITH-safe)
   hermes kanban list --status running --json > /tmp/kanban_running.json
   hermes kanban list --status done --json > /tmp/kanban_done.json
   hermes kanban list --status blocked --json > /tmp/kanban_blocked.json

2. CHECK WORKER AVAILABILITY
   ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
   Free = full_set(1-22) minus busy_set
   # If worker count > running task count, kill zombies first (see pitfall)
   # Also check for "results complete but not completed" tasks: reclaim + block immediately
   # See references/reclaim-then-block-for-results-complete.md
   #
   # HEARTBEAT CAVEAT: Python script workers (running exp_XXX.py directly, not
   # via hermes agent) do NOT send heartbeats. 0 heartbeats is NORMAL for these.
   # Use ps aux (Method 1) as ground truth for liveness, NOT heartbeat events.
   # Only hermes agent workers send heartbeats. See "Worker Liveness Check" section.

3. CHECK BLOCKED TASKS AND INFRASTRUCTURE
   For each blocked task: read comments to determine block reason.
   Group by reason:
     - Infrastructure (inference server unreachable, network issues): note in report, do NOT unblock
     - Duplicate: verify original is done, leave blocked
     - Other: assess if unblocking is appropriate
   If infrastructure blockers exist, note which experiment topics are blocked — this affects
   queue triage (don't create tasks for topics that need the blocked infrastructure).

4. DETECT UNSYNTHESIZED (Method A: done_exp_ids minus ss_exp_ids)
   If >=3 unsynthesized → CREATE SYNTHESIS TASK (always, regardless of saturation)
   If <3 unsynthesized:
     a) Check if synthesis task already running (filter by "synth" or "consolidat" in title)
     b) If synthesis running → add to running task via kanban_comment (if safe — see pitfall)
     c) If NO synthesis running → CREATE SYNTHESIS TASK (single experiments still need synthesis)
   # "Safe" = synthesis task is <5m old with 0 heartbeats, OR has recent heartbeats (<2m)
   # If synthesis task is >10m old with no heartbeats, create a separate synthesis task instead

5. CLASSIFY QUEUE ITEMS
   For each active (non-RESOLVED) item:
     source_ids = regex extract exp_NNN from text
     If source in running_exp_ids → RUNNING (skip)
     If source in ss_exp_ids → DONE (check if question answered)
     If no source → NO_SOURCE (always candidate)
   # Exclude items whose topics require blocked infrastructure (from step 3)

5.5. DETECT AND RECLAIM TOPIC DUPLICATES (maintenance — always, regardless of saturation)
   Use fuzzy keyword matching (see references/fuzzy-topic-duplicate-detection.md)
   For each duplicate pair:
     - If one task is >30min older: keep older, reclaim newer
     - If both <5min old: reclaim higher experiment number
     - Otherwise: keep both (complementary — see references/director-duplicate-judgment.md)
   Reclaim + block reclaimed tasks to prevent re-dispatch

6. CHECK SATURATION
   Verified_running = count of tasks confirmed running via kanban_show (not just list)
   If verified_running ≥ 7 → SYNTHESIS-ONLY, skip step 7
   EXCEPTION: if free_workers ≥ 3 AND 3+ genuinely OPEN queue items exist, create tasks (Cycle #158)
   # NOTE: Duplicate reclaim (see step 5.5) is MAINTENANCE, not investigation.
   # It happens regardless of saturation — free worker slots are a bonus, not the goal.
   #
   # CREATION-TO-SYNTHESIS TRANSITION: When queue is saturated (0 uncovered items at
   # 0.45 overlap threshold), switch to synthesis for remaining free workers.
   # See references/creation-to-synthesis-transition-pattern.md for worked example.

7. CREATE EXPERIMENT TASKS (only if below saturation)
   Priority order:
     a) NO_SOURCE items (pure research, always valuable)
     b) DONE/LOW-coverage items (source completed, question open, not covered by running tasks)
     c) DONE/MED-coverage items (partial coverage, lower priority)
   Never create tasks for RUNNING items (source experiment will resolve them)
   Never create tasks for topics blocked by infrastructure (inference server down)
   Assign to free workers only — never double-assign

8. DISPATCH
   hermes kanban dispatch
   (Spillover is normal — don't panic if fewer spawns than tasks created)

9. AUDIT LOG
   Write DIRECTOR PASS entry to self_audit.log
```

## What Changed from v186

1. **Step 1**: Now also dumps `--status blocked` (was missing)
2. **Step 3 (NEW)**: Checks blocked tasks and infrastructure status before making decisions. This prevents creating tasks for topics that need a down endpoint.
3. **Step 4**: When <3 unsynthesized, adds to running synthesis task via `kanban_comment` instead of skipping. Includes safety check for comment race condition.
4. **Step 5**: Now excludes queue items whose topics require blocked infrastructure.
5. **Step 5.5 (NEW, Cycle #232)**: Fuzzy topic duplicate detection and reclaim — happens regardless of saturation. Uses keyword overlap instead of exact string matching (see references/fuzzy-topic-duplicate-detection.md).
6. **Step 7**: Now excludes topics blocked by unreachable infrastructure.

## Rationale

In Cycle #228, the Director discovered 3 blocked tasks (exp_987, exp_993, exp_996) all blocked because an inference endpoint was unreachable. Without checking blocked tasks first, the Director might have created new tasks for topics that need the same unavailable infrastructure. The blocked task check is now a mandatory step that feeds into queue triage and task creation decisions.
