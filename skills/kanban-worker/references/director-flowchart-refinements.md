# Director Flowchart Refinements

## Added: Existing Synthesis Check (2026-06-01)

Step 3 of the Director Quick-Pass Flowchart should check for existing synthesis tasks before creating new ones:

```
3. DETECT UNSYNTHESIZED (Method A: done_exp_ids minus ss_exp_ids)
   If ≥3 unsynthesized:
     a) Check if synthesis task already running: filter kanban list for titles containing "synth" or "consolidat"
     b) If NO synthesis running → CREATE SYNTHESIS TASK (always, regardless of saturation)
     c) If synthesis already running → skip (don't duplicate — use kanban_comment to expand scope if needed)
   If <3 unsynthesized:
     a) Check if synthesis task already running
     b) If synthesis running → add to running task via kanban_comment (if safe — see race condition pitfall)
     c) If NO synthesis running → CREATE SYNTHESIS TASK (single experiments are still worth synthesizing)
```

**Rationale:** Without this check, the Director can create duplicate synthesis tasks that waste a worker slot. In Cycle #213, the board was fully saturated (22/22) and a synthesis task was already running — the Director correctly skipped creating another.

**Gap fixed (2026-06-01):** The original flowchart said "If <3 unsynthesized → skip synthesis this pass" — but this is wrong when no synthesis task is running. A single unsynthesized experiment (exp_1007) should still be synthesized. The correct behavior: if <3 unsynthesized AND no synthesis task exists, create one. Only skip when a running synthesis task already covers the experiments.

**Pattern:** Before creating ANY new synthesis task, check `hermes kanban list --status ready --status running --json` for existing synthesis tasks (filter by title containing "synth" or "consolidat"). If one exists and covers the same experiments, use `kanban_comment` to expand its scope instead of creating a duplicate.

## Added: Orphaned Task Detection via Process Cross-Reference (2026-06-01)

A fast bulk check for status-desynced tasks (done but still showing running in list):

```bash
# Count running tasks from kanban list
RUNNING_COUNT=$(hermes kanban list --status running --json 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")

# Count active hermes agent processes
AGENT_COUNT=$(ps aux | grep 'hermes.*kanban' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u | wc -l)

# If RUNNING_COUNT > AGENT_COUNT, there are orphaned tasks (done but desynced)
if [ "$RUNNING_COUNT" -gt "$AGENT_COUNT" ]; then
    echo "WARNING: $((RUNNING_COUNT - AGENT_COUNT)) orphaned tasks (done but showing running)"
    # Verify with individual kanban show for each suspected orphan
fi
```

**Rationale:** This is faster than calling `kanban show --json` for every running task. It catches the common case where a task completes (process exits) but the list view doesn't update status. In the 2026-06-01 Director pass, this caught synthesis task t_2f3918c5 — done (confirmed by `kanban show`) but still showing as running in the list.

**When to use:** At the start of every Director pass, alongside the worker availability check. If orphaned tasks are found, verify each with `kanban show --json` to confirm completion, then note them in the audit log. No action needed — the dispatcher will eventually reconcile.

## Added: Scorer + Classification Ordering (2026-06-02)

**Problem:** The curiosity scorer runs on ALL queue items (63 in this pass), but most are already covered by running experiments. Running the scorer on covered items wastes time and produces misleading output — high scores for items that shouldn't get tasks.

**Correct ordering:**

```
Step 4a: CLASSIFY queue items → filter for genuinely uncovered
  - NO_SOURCE with LOW coverage (<0.15) → always candidate
  - DONE/ACTIVE with LOW coverage → candidate if question not answered
  - RUNNING items → skip (will resolve when experiment completes)
  - RESOLVED items → skip

Step 4b: RUN SCORER on uncovered subset only
  - python3 ~/.hermes/scripts/curiosity_scorer.py --json
  - Filter scored output to only the uncovered item indices from 4a
  - Apply score threshold (≥60) to the filtered subset

Step 4c: CREATE TASKS for items passing both filters
  - Score ≥60 AND (NO_SOURCE with LOW coverage) or (DONE/ACTIVE with LOW coverage)
  - Respect thread diversity cap (35% max per thread)
```

**Why this order matters:**
- Scorer on 63 items → 2 genuinely uncovered → wastes 61 scorings
- Classification on 63 items → 2 uncovered → then scorer on 2 → correct prioritization
- The scorer's output is only actionable for items that pass the classification filter

**Real example (2026-06-02):** Scorer returned 63 items. Queue triage found only 2 genuinely uncovered (items 53 and 62, score=49). Both below the 60 threshold. Correct decision: no new tasks created despite 3 free workers. Without the ordering, the Director might have created tasks for high-scoring items that are already covered by running experiments.

**Interaction with score threshold + free workers:** Having free workers does NOT override the score threshold. If uncovered items score <60, don't create tasks — the free workers will be picked up by the next dispatch tick for ready tasks. The threshold exists to prevent low-value investigation that wastes API budget.

## Added: Two-Pass Status Desync Re-verification (Cycle #245, 2026-06-02)

**Problem:** Step 2 of the flowchart ("CHECK WORKER AVAILABILITY") only mentions zombie cleanup when "worker count > running task count". But Cycle #245 revealed 93% status desync (14/15 "running" tasks were actually done) even when worker count (22) was LESS than list count (15). The conditional check misses the common case.

**Updated step 2:**

```
2. CHECK WORKER AVAILABILITY + STATUS DESYNC RE-VERIFICATION
   ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
   Free = full_set(1-22) minus busy_set

   2a. ZOMBIE CLEANUP: If worker count > running task count, kill zombie processes
       (see zombie pitfall). Cross-reference ps aux against kanban list to identify
       which PIDs to kill.

   2b. STATUS DESYNC RE-VERIFICATION (MANDATORY — do NOT skip even when counts match):
       After killing zombies, spot-check 3-5 "running" tasks with `kanban show <id> --json`.
       The list endpoint has 93% desync rate observed (14/15 tasks actually done).
       Tasks can also flip between blocked/ready across checks.
       Use verified_running count (from kanban show), NOT list count, for saturation decisions.
       See references/status-desync-93pct-rate-cycle245.md for the full pattern.

   2c. Also check for "results complete but not completed" tasks: reclaim + block immediately.
       See references/reclaim-then-block-for-results-complete.md
```

**Why this matters:**
- List count (15) suggested saturation → would skip task creation
- Verified count (9) is below threshold → allows task creation
- Free worker count: list suggests 7, verified suggests 13
- Creating tasks for desynced-done tasks wastes worker slots

**Real example (Cycle #245):** After killing 10 zombies, 5 tasks remained in the "running" list. Spot-checking revealed 1 more was actually blocked/ready (not running). True running count: 4 (later verified as 9 after recheck). Without re-verification, the Director would have created tasks for 3 desynced-done tasks, wasting 3 worker slots.
