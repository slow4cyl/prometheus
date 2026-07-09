# Director Pass: False SKIP Signal & Oversized Synthesis Scope (Cycle ~583)

## False SKIP Signal from Pre-Run Script

The pre-run script's timestamp comparison can produce incorrect SKIP signals.

**Example output:**
```
SKIP: self_state.json updated -1351s ago (< 90s threshold). Waiting for current cycle to complete.
```

The value -1351s (22.5 minutes) is clearly above the 90s threshold, but the script incorrectly treats it as below. This is a comparison logic bug in the pre-run script.

**Detection:** The SKIP message shows a large negative or positive number (>90) but still says "< 90s threshold". The sign may be negative (elapsed time inverted) or the comparison operator may be wrong.

**Fix:** Ignore the SKIP signal and verify self_state.json mtime directly:
```bash
stat ~/.hermes/self_state.json | grep Modify
```
If mtime is >90s old, proceed with the full investigation cycle. Trust the filesystem, not the script output.

**Impact:** Without this check, the Director skips the entire investigation cycle (Steps 3-8) when it should be running. Free workers sit idle, unsynthesized experiments pile up, and queue items go uncovered.

**Relationship to existing references:**
- `director-pass-skip-threshold-with-free-workers.md` assumes SKIP is genuine
- `pre-run-script-staleness-pitfall.md` covers stale DATA but not false SKIP SIGNALS
- This pitfall covers the case where the SKIP itself is wrong

## Oversized Synthesis Task Scope Hang

A synthesis task claiming to consolidate 1000+ experiments will hang indefinitely.

**Example:** Task t_4af67875 titled "SYNTHESIS: consolidate 1716 unsynthesized experiments"
- Ran for 72 minutes
- 0 events (no heartbeats, no progress)
- 1 second total CPU time (process alive but completely idle)
- Process confirmed alive via `ps aux`

**Root cause:** The scope is too large for a single synthesis pass. The synthesis worker attempts to read and process all 1716 experiments, gets stuck on API calls or memory, and never produces output.

**Secondary issue:** The claimed count was wrong. The `experiments` field in self_state.json is an integer (2194), not a dict with `completed` array. The unsynthesis detection code that assumes dict format produces a false "1716 unsynthesized" count when `ss_exp_ids` stays empty.

**Detection:**
1. `kanban show --json` → 0 events after 30+ minutes
2. `ps -p <pid> -o time` → <5s CPU after 30+ minutes elapsed
3. Workspace has only the original script, no output files

**Fix:**
1. Verify actual unsynthesized count: `done_exp_ids - ss_exp_ids` with proper integer-format handling
2. Reclaim: `hermes kanban reclaim <task_id>`
3. Block: `hermes kanban block <task_id> "hung: oversized scope (claimed N, actual M), 0 events, <5s CPU"`
4. If genuine unsynthesized experiments exist, create smaller synthesis tasks (max 20-30 experiments each)

**Prevention:**
- Always check actual unsynthesized count BEFORE creating synthesis tasks
- Use `experiments_completed_list` from metrics when `experiments` is an integer
- Cap synthesis task scope at 20-30 experiments per task
- List experiment IDs explicitly in the task body

## Topic Duplicates Slip Through Diversity Cap

The 35% thread diversity cap prevents thread monopolization but NOT semantic duplicates within a thread.

**Example:** 4 topic duplicate groups found among 17 running tasks:
1. "Cross-dataset few-shot adaptation" — 3 tasks (exp_2410, exp_2434, exp_2450)
2. "Standalone TF-IDF+LR wins at 100+ domains" — 2 tasks (exp_2426, exp_2453)
3. "Ensemble disagreement AUC 0.84-0.97" — 2 tasks (exp_2428, exp_2455)
4. "Isotonic calibration production-scale" — 2 tasks (exp_2435, exp_2451)

Total: 9 tasks across 4 groups, 5 were duplicates.

**Why it happens:** The batch creator's `is_already_in_queue()` catches queue-level duplicates (same text appearing twice in the queue), but not running-task-level duplicates (different queue entries that ask the same question with different phrasing).

**Detection (after task creation):**
```python
from collections import defaultdict
import re
topics = defaultdict(list)
for t in running:
    m = re.search(r'exp_\d+:\s*(.+)', t.get('title', ''))
    if m:
        topic = m.group(1)[:80].lower().strip()
        topics[topic].append(t['id'])
for topic, ids in topics.items():
    if len(ids) > 1:
        print(f"DUPLICATE ({len(ids)}): {topic} → {ids}")
```

**Fix:** Reclaim the newer duplicates (higher exp numbers) and block with reason noting the duplicate. The older task retains the workspace and history.

**Prevention:** Before batch task creation, run the topic dedup check on candidate items. Filter out items whose normalized topic matches an already-running task's topic.
