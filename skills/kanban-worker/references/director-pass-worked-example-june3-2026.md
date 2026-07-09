# Director Pass Worked Example — June 3, 2026 (Cron Cycle)

Concrete example of a clean Director pass with batch task creation, synthesis, and post-creation verification.

## Board State at Start

```
Running tasks: 26 (24 unique workers)
Free workers: 6 (12, 13, 19, 21, 31, 9)
Stuck (>25min): 7 (all confirmed running via kanban show, within max_runtime)
Unsynthesized: 15 (after proper filtering — see inflation note below)
Queue: 134 items (116 active, 18 resolved)
Top threads: injection(15), tfidf(13), attack(11), calibration(6), embedding(5)
```

## Step 1: Unsynthesized Count — Inflation from Non-Experiment Done Tasks

Initial scan counted **30** unsynthesized experiments. After proper filtering (matching only `exp_NNN:` title pattern, excluding synthesis tasks and other non-experiment done tasks), the count dropped to **15**.

**Root cause:** The naive filter `title LIKE 'exp_%'` matches synthesis tasks that reference experiments in their titles (e.g., `SYNTHESIS: consolidate exp_2708-exp_2855`), inflating the done set. When these are subtracted from the self_state completed list, phantom "unsynthesized" entries appear.

**Fix:** Use a stricter regex that matches only experiment-format titles:
```python
# WRONG — catches synthesis tasks referencing experiments
rows = conn.execute("SELECT title FROM tasks WHERE status='done' AND title LIKE 'exp_%'").fetchall()

# RIGHT — matches only actual experiment titles (exp_NNN: <hypothesis>)
import re
rows = conn.execute("SELECT title FROM tasks WHERE status='done'").fetchall()
exp_titles = [r[0] for r in rows if re.match(r'^exp_\d+:', r[0])]
```

**Lesson:** Always verify unsynthesized count against known experiment ranges. If the count seems high (>20), check for non-experiment tasks inflating the done set.

## Step 2: Batch Task Creation

```
batch_create_tasks.py --count 6 --min-score 30
→ 6 tasks created, diversity cap: 2/thread
→ Thread distribution: injection(2), tfidf(2), embedding(1), calibration(1)
```

All 6 tasks dispatched and started running immediately. No deferrals, no conflicts.

**Notable:** One task (exp_2859, worker-9) completed before dispatch returned — worker was fast enough to finish during the dispatch RPC. This is normal and not a sign of failure.

## Step 3: Synthesis Task

Created synthesis task for 15 unsynthesized experiments (exp_2708–exp_2855). Assigned to `prometheus-synthesis`. Dispatched and running.

## Step 4: Post-Creation Verification

After dispatch, verified all 7 new tasks (6 experiments + 1 synthesis) were running:
- 6/6 experiment tasks: RUNNING ✓
- 1/1 synthesis task: RUNNING ✓
- 1 experiment task (exp_2859) already completed during pass ✓

## Step 5: Audit + WM Update

- Audit log written to `self_audit.log`
- WM daemon focus updated to `director_pass_2026_06_03_cycle245`
- WM associations added for new experiments

## Anti-patterns Avoided

1. **Reclaiming stuck tasks that are actually running:** All 7 "stuck" tasks (>25min) were confirmed running via `kanban show`. Would have wasted time reclaiming legitimate long experiments.

2. **Creating tasks for covered queue items:** Batch creator's diversity cap + min-score filtering avoided duplicating running experiment topics.

3. **Synthesizing when workers are free:** Created 6 experiment tasks first (creation over synthesis), then 1 synthesis task. Correct priority order.

## Metrics

| Metric | Before | After |
|--------|--------|-------|
| Running tasks | 26 | 30 |
| Free workers | 6 | 0 (all assigned) |
| Uns synthesized | 15 | 15 (synthesis task created, not yet run) |
| Queue active | 116 | 110 (6 items consumed by new tasks) |
