# Director Pass Worked Example — Cycle 245

Concrete example of a Director pass with status desync detection, queue classification, and task creation decisions.

## Board State at Start

```
Running: 7 tasks (kanban list)
Done: 1854 tasks
Free workers: 17 of 22
Unsynthesized: 1 (exp_1549 — below 3-threshold)
Queue: 32 items, 6 OPEN after classification
```

## Step 1: Status Desync Detection

`kanban list --status running` returned 7 tasks. `ps aux | grep prometheus-worker` found only 6 unique worker profiles. Cross-referencing revealed:

```python
# t_0abd3a88 (exp_1551) showed as "running" in list
# But kanban show confirmed: status=done, summary available
# Process was dead (ps aux returned nothing for this task ID)
```

**Lesson:** Always verify ambiguous tasks with `kanban show <id> --json` when the running count is near the saturation threshold. The list endpoint doesn't update status in real-time after `kanban_complete`.

## Step 2: Queue Classification

16 non-RESOLVED items classified:

| Category | Count | Action |
|----------|-------|--------|
| COVERED by running tasks | 10 | Skip — will resolve when running experiment completes |
| OPEN (distinct thread) | 6 | Create tasks |
| RESOLVED | 16 | Already tagged |

**Key decision:** 3 of the 4 highest-scored items (≥60) were COVERED by running tasks:
- exp_1553 (INT1 standalone) → already running as t_e29fcf64
- exp_1532 (character calibration transfer) → already running as t_23d9b9e6
- exp_1551 (native-vocabulary attacks) → already done (status desync)

Only 1 high-score item was genuinely OPEN. The 6 OPEN items clustered into 3 research threads:
1. Embedding modality gating (3 items — same topic, 1 task)
2. Cascade break-even (2 items — same topic, 1 task)
3. QAT tradeoff (1 item, 1 task)

**Lesson:** Score alone doesn't determine task creation. Cross-reference against running tasks FIRST. Duplicate tasks waste worker slots.

## Step 3: Task Creation

Created 3 tasks for 3 distinct threads, assigned to free workers:

```
exp_1554: Script-aware embedding gating → worker-3 (score=54, 3 queue items covered)
exp_1555: Cascade break-even negative correlation → worker-8 (score=38, 2 items covered)
exp_1556: QAT ID/OOD tradeoff → worker-9 (score=43, 1 item covered)
```

**Thread diversity:** 3 tasks across 3 threads = 33% each (well under 35% cap).

## Step 4: Synthesis Decision

1 unsynthesized experiment (exp_1549) — below the 3-threshold. Correctly skipped synthesis in favor of task creation (priority rule: creation over synthesis when workers are free).

## Anti-patterns Avoided

1. **Creating tasks for COVERED queue items:** Would have created 4 duplicate tasks (exp_1553, exp_1532, exp_1551, and one embedding gating duplicate) if only looking at scores without cross-referencing running tasks.

2. **Creating a synthesis task for 1 unsynthesized experiment:** Wastes a worker slot when 16 are free and 6 queue items need investigation.

3. **Trusting kanban list status:** Would have treated t_0abd3a88 as stuck and potentially reclaimed a done task.
