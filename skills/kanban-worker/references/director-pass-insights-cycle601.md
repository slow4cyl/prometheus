# Director Pass Checklist — Cycle #601 Insights

## Key Learnings from Cycle #601

### 1. Coverage Threshold Tuning

The `batch_create_tasks.py` script's coverage thresholds were too aggressive:
- Old: hypothesis > 0.4, result > 0.65
- New: hypothesis > 0.6, result > 0.75

**Why this matters:** Follow-up experiments naturally share significant word overlap with their source experiments. The old thresholds filtered out legitimate follow-ups as "covered".

### 2. Synthesis Worker Behavior

The synthesis worker only synthesizes experiments that are:
1. In the done list (kanban tasks marked as done)
2. NOT already in self_state.json

**Example from Cycle #601:**
- 5 unsynthesized experiments identified
- Synthesis worker synthesized 2 (exp_2172, exp_2178)
- Other 3 (exp_2160, exp_2168, exp_2177) were already synthesized in a previous cycle

**Lesson:** Always check if experiments are already synthesized before creating a synthesis task.

### 3. Manual Task Creation Pitfall

Manually creating tasks bypasses important dedup checks:
- Batch creator's coverage logic
- Intra-batch dedup
- Thread diversity cap

**Example from Cycle #601:**
- Manually created 9 tasks (exp_2189-exp_2197)
- All had high overlap with completed experiments
- Workers already processing them
- API budget being spent on potentially duplicate work

**Lesson:** Trust the batch creator's logic unless you've verified it's being too conservative.

### 4. Post-Creation Duplicate Check

Always run the post-creation duplicate check (Step 7 in the checklist) to catch:
- Topic duplicates that the batch creator's Jaccard check missed
- Same-topic different-ID duplicates
- Reclaimed duplicates that get re-dispatched

### 5. Stuck Task Detection

Tasks with 0 heartbeats are NOT necessarily stuck:
- Python scripts don't send heartbeats (only hermes agents do)
- 0 heartbeats is NORMAL for healthy script workers
- Use `ps aux` as ground truth for process liveness

**Detection pattern:**
1. Check process liveness: `ps aux | grep <task_id>`
2. Check workspace output: file modification times
3. Check CPU time: subprocess with 0 CPU time for 30+ min is hung

## Updated Checklist Steps

### Step 3: Classify Queue Items

```python
# Check against completed experiments, not just running tasks
# Use higher thresholds: hypothesis > 0.6, result > 0.75
# Items with overlap between 0.4-0.6 are likely follow-ups, not duplicates
```

### Step 5: Check for Unsynthesized Experiments

```python
# Method A: done_exp_ids minus ss_exp_ids
# Exclude synthesis task titles from extraction
# Sort by experiment number using regex key
# Verify against audit trail before creating synthesis task
```

### Step 7: Post-Creation Duplicate Check

```python
# Mandatory after batch_create_tasks.py creates tasks
# Check for topic duplicates in running + ready tasks
# Reclaim newer duplicates, block with reason
```

### Step 9: Check for Stuck Tasks

```python
# Process liveness: ps aux (ground truth)
# Workspace check: file modification times
# CPU time: subprocess with 0 CPU for 30+ min is hung
# Don't rely on heartbeats for script workers
```

## Metrics from Cycle #601

- Experiments completed: 2054
- Synthesis cycles: 447
- Self-modifications: 86
- Director passes: 601
- Queue items: 48 (25 with score ≥ 60)
- Running tasks: 15
- Done tasks: 2445
