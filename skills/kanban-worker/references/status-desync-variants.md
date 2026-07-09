# Status Desync Variants — Complete Reference

Seven distinct desync patterns have been observed between `kanban list`, `kanban show`, `kanban reclaim`/`kanban block`, and actual process state.

## Variant 1: Status Mismatch (Cycle #145, #161, #180)

Task shows `status=running` in `kanban list --json` but actually `status=done` in `kanban show`.

**Cause:** List view doesn't update in real-time after `kanban_complete`.

**Detection:** `kanban show <id> --json` shows `latest_summary` or `runs[].summary` with completion data.

**Impact:** Director may try to block/complete an already-done task. `kanban block` fails with "cannot block".

## Variant 2: Zombie Processes (Cycle #180)

Task is `status=done` but hermes agent process is still alive (`ps aux` confirms).

**Cause:** Process doesn't exit immediately after `kanban_complete`.

**Detection:** `kanban show` says done, but `ps aux | grep <task_id>` shows live process.

**Impact:** Wastes worker profile slot. Harmless otherwise. Kill with `kill <PID>`.

## Variant 3: List Omission (Cycle #197)

Task is `status=running` (confirmed via `kanban show` and `ps aux`) but completely ABSENT from `kanban list --json`.

**Cause:** Unknown — possibly list endpoint caching or partial index corruption.

**Detection:** Cross-reference `ps aux` kanban task IDs against list output. Any task in `ps aux` but not in list is invisible.

**Impact on Director:**
- Saturation count undercounts (14 in list vs 15 actual)
- Free worker count overstates (8 calculated vs 7 true)
- May create tasks overlapping with invisible runner's work

**Fix:** Use `ps aux` as ground truth for saturation decisions, not list count. When running count is near threshold (6-8), always verify with `ps aux`.

**Downstream impact on unsynthesis detection:** Variant 3 list omission also inflates the "unsynthesized" count in Method A detection. See `references/unsynthesis-detection-variant3-impact.md` for the full analysis and fix.

**Example (Cycle #197):**
```
kanban list --status running --json → 14 tasks
ps aux | grep 'kanban task' → 15 unique task IDs
Invisible: t_8698857a (exp_560, worker-15)
True free workers: 7 (not 8)
```

## Variant 4: NO WORKSPACE on Old Running Task (Cycle #200)

Task shows `status=running` in `kanban list --json` and is >30m old, but `os.path.isdir(workspace)` returns False — the workspace directory does not exist.

**Cause:** Task completed quickly (possibly trivial work or auto-completed), workspace was garbage-collected, but the list endpoint still shows it as running. Alternatively, the task was dispatched but never actually started (dispatcher assigned but worker never claimed the workspace).

**Detection:** During bulk workspace health check (Method 3), a task returns "NO WORKSPACE" instead of ACTIVE/SLOW/STALE. Cross-reference with `kanban show <id> --json` — if status=done with a summary, it's a completed task with desynced list status.

**Impact on Director:**
- Inflates the running count (task counted as running but is actually done)
- May cause saturation threshold to be applied when true count is lower
- Free worker count is understated

**Diagnostic pattern (added to workspace health check):**
```python
if not os.path.isdir(ws):
    # Don't immediately assume desync — check kanban show for actual status
    # New tasks (just dispatched) legitimately have no workspace yet
    # Only flag if task is >30m old
    if age_min > 30:
        print(f'{tid}: NO WORKSPACE (>30m old — likely desynced to done)')
    else:
        print(f'{tid}: NO WORKSPACE (newly dispatched — normal)')
    continue
```

**Real-world example (Cycle #200):** exp_569 (t_5f6bfc8d) showed as running in list (66m old), workspace check returned NO WORKSPACE. `kanban show --json` confirmed status=done with summary about anti-manipulation framing mechanism findings. The task had completed but list status never updated. True running count was 17, not 18.

**Cross-reference with Variant 1:** This is a specific case of Variant 1 (status mismatch) where the workspace signal is the first detection method. The workspace check (Method 3) is faster than individual `kanban show` calls for bulk detection.

## Variant 5: Desynced Done Tasks Invisible to Uns Synthesized Detection (Cycle #231)

Task is `status=done` (confirmed via `kanban show`) but appears as `status=running` in `kanban list --json`. Because the done list only includes tasks with `status=done` in the list endpoint, desynced tasks are ABSENT from the done list.

**Impact on Director:** The most dangerous variant for synthesis coverage. Method A unsynthesized detection extracts experiment IDs from done task titles (`kanban list --status done --json`). Desynced done tasks are NOT in this list, so their experiment IDs are never extracted. The synthesis worker never processes them — they become permanently unsynthesized gaps.

**Detection:** After running Method A, cross-reference the running list against `kanban show` for each task. Any task that shows `status=done` in `kanban show` but `status=running` in the list is a desynced done task. Its experiment ID must be manually added to the unsynthesized set.

```python
import json, subprocess

running = json.load(open('/tmp/kanban_running.json'))
desynced_done = []

for t in running:
    tid = t['id']
    result = subprocess.run(['hermes', 'kanban', 'show', tid, '--json'],
                          capture_output=True, text=True, timeout=10)
    try:
        d = json.loads(result.stdout)
        actual_status = d.get('task', {}).get('status', '?')
        if actual_status == 'done':
            desynced_done.append(tid)
    except:
        pass

print(f"Desynced done tasks: {len(desynced_done)}/{len(running)}")
# Add their experiment IDs to the unsynthesized set manually
```

**Real-world example (Cycle #231):** exp_1076 (t_abdc05c3) and exp_1086 (t_b5eef554) both showed as running in the list but were done in `kanban show`. Method A found only 4 unsynthesized (exp_1072, exp_1075, exp_1079, exp_1080). After detecting the desync, the true count was 6 — the 2 desynced tasks' experiments were invisible to the done-list extraction.

**Fix:** After Method A, run a desync check on running tasks. For each desynced done task, extract its experiment ID from the title and add it to the unsynthesized set. Include all experiments in the synthesis task body.

**Relationship to saturation:** Desynced done tasks inflate the running count (Variant 1) AND hide experiments from synthesis (this variant). The double impact means both the saturation decision and the synthesis coverage are wrong.

## Variant 6: Completed Synthesis Task Still in Running List (Director #246)

Synthesis task `kanban show --json` reports `status=done` with a summary, but `kanban list --status running --json` still includes it. The Director sees it as "running" and skips creating synthesis (or creates a duplicate).

**Cause:** Same as Variant 1 — list endpoint doesn't update after `kanban_complete`. Synthesis tasks complete fast (<2 min), so the desync window is large relative to task lifetime.

**Detection:** Before creating any synthesis task, extract synthesis-related task IDs from the running list and spot-check each with `kanban show --json`. If `status=done`, the synthesis is already complete — do NOT create a replacement.

**Impact on Director:** Wastes a worker slot on a duplicate synthesis task that covers already-synthesized experiments. In Director #246, this would have created a second synthesis for exp_1355 (already consolidated by t_f7e25100).

**Fix in Director quick-pass flowchart:** After step 1 (dump board state), add: "For any task in running list with 'synth' or 'consolidat' in title, verify with `kanban show --json`. If done, exclude from saturation count and skip synthesis creation for its experiments."

**Relationship to existing pitfall:** This is the *pass-spanning* variant of the "Duplicate synthesis task in same Director pass" pitfall (Cycle #161). That pitfall covers creating two synthesis tasks in one pass; this variant covers a synthesis task from a *previous* pass that desyncs into the current pass's running list.

---

## Variant 7: List-CLI Status Desync (Director #249)

Task shows `status=running` in `kanban list --json` but both `kanban reclaim` and `kanban block` reject it with "not running or unknown id" / "cannot block". `kanban show --json` confirms `status=running` with 0 events and no summary.

**Cause:** The task's internal state is inconsistent — the list endpoint reads one source of truth while the reclaim/block CLI reads another. Likely a race condition where the task was partially created or dispatched but never fully initialized in the mutation layer.

**Detection:** Task appears in running list, `kanban show` says running, but CLI mutations (reclaim, block) fail. The task has 0 events and no worker process in `ps aux`.

**Impact on Director:**
- Task occupies a slot in the running count but cannot be reclaimed or blocked
- No worker process exists — it's a phantom running task
- Inflates saturation count (counts toward ≥7 threshold)
- Cannot be cleaned up via normal Director tools

**Diagnostic pattern:**
```python
# After detecting a task with 0 events and no worker process:
result = subprocess.run(['hermes', 'kanban', 'reclaim', task_id],
                       capture_output=True, text=True, timeout=10)
if 'not running' in result.stdout or 'cannot' in result.stdout:
    # Variant 7: List says running but CLI rejects mutations
    # Fall back to ignoring it — the task is stuck in an unrecoverable state
    print(f"VARIANT 7: {task_id} — stuck in list-CLI desync, cannot clean up")
```

**Real-world example (Director #249):** Synthesis task t_a4bb2ff5 ("consolidate exp_1511, exp_1513") showed as running in list, `kanban show` confirmed status=running with 0 events and no worker process. Both `reclaim` and `block` failed. The task was left in place — no cleanup possible via Director tools. The Director proceeded with task creation for free workers, treating the phantom as part of the running count.

**Fix:** There is no Director-side fix. The task is stuck until the operator intervenes or the database is cleaned manually. When encountered, count the phantom task in the saturation calculation but do not waste cycles trying to reclaim or block it. Note it in the audit log for operator awareness.

**Relationship to existing variants:** This is distinct from Variant 1 (list says running, show says done) — here BOTH list AND show say running, but the mutation layer rejects the task. It's also distinct from Variant 2 (zombie processes) — there IS no process. The root cause is likely a database state inconsistency rather than a caching issue.

## Variant 8: Status Fluctuation Between Checks (Cycle #245, 2026-06-02)

Task shows `status=blocked` in one `kanban show --json` check but `status=ready` in a subsequent check seconds later. The status appears to fluctuate between checks without any Director intervention.

**Cause:** Unknown — possibly a race condition where the task is transitioning between states (e.g., blocked → unblocked by operator, or dispatcher re-processing). May also be a read-your-own-writes inconsistency in the SQLite layer.

**Detection:** When spot-checking running tasks, a task's status changes between two `kanban show` calls separated by a few seconds. Example:
```
Check 1: t_36484f11 → status=blocked
Check 2: t_36484f11 → status=ready (10 seconds later)
```

**Impact on Director:**
- May cause the Director to misclassify the task (e.g., treat a ready task as blocked and skip it)
- If the Director acts on the first check (e.g., tries to reclaim a "blocked" task), the action may fail or produce unexpected results

**Fix:** Don't act on a single status check for ambiguous tasks. If status seems inconsistent, recheck after a few seconds. Use the second check as ground truth. In Cycle #245, the task was ultimately verified as `status=running` on the third check — the blocked/ready fluctuation was transient.

**Relationship to existing variants:** This is distinct from Variant 1 (stable mismatch) — here the status actively changes between checks. It's most similar to Variant 7 (list-CLI desync) in that the root cause is likely a database state inconsistency, but the manifestation is different (fluctuation vs permanent rejection).

## Cross-cutting Rule

**Always use `ps aux` as ground truth for worker liveness and task count.** The list endpoint is unreliable for:
- Status accuracy (variant 1, 4, 8)
- Process state (variant 2)  
- Task presence (variant 3)

**Bulk detection shortcuts (fastest to slowest):**
1. `ps aux` — ground truth for process liveness and task count
2. Workspace file mtime check — catches variant 4 (NO WORKSPACE on old task) in bulk
3. `kanban show <id> --json` — reliable for individual status but N calls for N tasks

The `kanban show <id> --json` endpoint is reliable for individual task status but slow for bulk checks (requires N API calls for N tasks).

**Note on 93% desync rate (Cycle #245):** In one Director pass, 14 out of 15 tasks in `kanban list --status running` were actually done. This is the highest desync rate observed. The two-pass verification pattern (kill zombies → recheck remaining tasks) is now mandatory in the Director flowchart. See `references/status-desync-93pct-rate-cycle245.md` for the full analysis.
