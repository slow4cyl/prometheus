# batch_create_tasks.py Conservative Scoring Pitfall

## Problem (Cycle #248)

The `batch_create_tasks.py` script scores queue items and filters by a score threshold (≥60), then applies a 35% thread diversity cap. In Cycle #248, it scored 22 active queue items but found only 3 above threshold, then after diversity cap created just 1 task — while 12 workers sat idle.

## Root Cause

The scoring function penalizes items that share keywords with running tasks, even when the research questions are genuinely distinct. For example:
- Queue item: "TF-IDF+LR vocabulary pruning at 100+ domains"
- Running task: "TF-IDF+LR domain-independent vocabulary"
- Shared keywords: "TF-IDF", "LR", "vocabulary", "domain"
- Score penalty: high overlap → low score → filtered out

But the research questions are genuinely different: one asks about pruning strategies, the other about structural features.

## Detection

After running `batch_create_tasks.py`, check if `Created N/12 tasks` (or similar) where N < 50% of free workers. **BUT** — this is only a problem when the queue genuinely has uncovered items. See "Convergence vs Conservatism" below.

## Convergence vs Conservatism (Cycle #248, 2026-06-02)

Low task creation is NOT always a bug. Two distinct scenarios produce the same signal:

**Scenario A — True Conservatism:** Queue has genuinely uncovered NO_SOURCE items, but the batch creator's coverage scoring (Jaccard overlap with running titles) falsely marks them as covered. Example: "cross-domain feature transfer theoretical ceiling" shares keywords with "script-aware gating generalizes across embedding models" but asks a different question.

**Scenario B — Queue Convergence:** Running tasks genuinely cover the queue. With 16 running tasks, 50 NO_SOURCE queue items, the batch creator correctly identifies that running experiments will answer the queue items when they complete.

**How to distinguish:**
1. Run the batch creator with `--dry-run` first
2. Check `Uncovered by running tasks: N`
3. If N ≈ 0 AND you have ≥7 running tasks → **convergence** (correct behavior, don't create tasks)
4. If N ≈ 0 AND you have <7 running tasks → **conservatism** (run manual fallback)
5. If N > 0 but < free_workers → create the N tasks, leave remaining workers idle

**Rule:** When ≥7 running tasks AND batch creator finds <3 uncovered items, the queue is likely converged. Create a synthesis task (maintenance) but do NOT create experiment tasks just to fill free slots. Idle workers are correct when there's nothing genuinely uncovered.

## "Uncovered: 0" False Negative (Director Pass #577, Cycle #293)

The batch script can report `Uncovered by running tasks: 0` even when many genuinely uncovered NO_SOURCE items exist. This is the most extreme form of conservatism — the Jaccard overlap scoring marks items as "covered" when they share keywords with running tasks but ask fundamentally different research questions.

**Concrete example:**
```
$ python3 ~/.hermes/scripts/batch_create_tasks.py --count 10 --dry-run
Queue: 13 active items, 0 score >= 60
Workers: 9 free, 13 busy
Uncovered by running tasks: 0
Selected: 0 tasks (diversity cap: 3/thread)
```

But manual classification found 18 NO_SOURCE items with coverage scores 0.00–0.14 (all below the HIGH threshold of 0.30):
- `cov=0.00`: "Does multi-class routing generalize beyond 60 domains to production scale?"
- `cov=0.00`: "Script-aware gating FPR still high — can we reduce false positives?"
- `cov=0.00`: "GPT-4o's zero-regression behavior — RLHF or architectural?"
- `cov=0.08`: "FDA Food Labeling Q6 and Q10 fail on ALL models — inherently ambiguous?"
- (13 more items)

**Why the script fails here:** The coverage scoring normalizes queue item keywords against running task titles. Items with generic keywords ("model", "domain", "features", "test") overlap heavily with ANY running task title, even when the specific research question is completely different. The script's keyword matching can't distinguish "routing generalizes beyond 60 domains" from "gating generalizes across embedding models" — both share "generalizes" and "domains".

**Manual fallback pattern (proven in this session):**
```python
import re, json, os

running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '') for t in running]
running_exp_ids = set()
for t in running:
    for m in re.finditer(r'exp_(\d+)', t.get('title', '')):
        running_exp_ids.add(f'exp_{m.group(1)}')

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'from'}

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    source_ids = re.findall(r'exp_(\d+)', text)
    if source_ids and any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
        category = "RUNNING"
    elif source_ids:
        category = "DONE_NO_TASK"
    else:
        category = "NO_SOURCE"
    
    if category == "NO_SOURCE":
        words = set(text.lower().split())
        keywords = {w for w in words - stop if not re.match(r'^exp_\d+$', w) and len(w) > 2}
        running_text = ' '.join(t.lower() for t in running_titles)
        overlap = sum(1 for w in keywords if w in running_text)
        coverage = overlap / max(len(keywords), 1)
        if coverage < 0.15:
            print(f"[{i}] {category} (cov={coverage:.2f}) {text[:90]}")
    elif category == "DONE_NO_TASK":
        print(f"[{i}] {category} {text[:90]}")
```

**Result from Director Pass #577:**
- Batch script: 0 tasks created
- Manual fallback: 10 tasks created (exp_2047–2056)
- All dispatched successfully, filling 10 of 12 free worker slots

**Rule update:** When `batch_create_tasks.py --dry-run` reports "Uncovered: 0" but you have ≥3 free workers AND the queue has ≥10 items, ALWAYS run the manual NO_SOURCE classification before concluding convergence. The script's coverage scoring is too aggressive for diverse research queues.

## Fix: Manual Fallback

When the batch script produces fewer tasks than free workers:

1. **Classify queue items manually** using the RESOLVED/RUNNING/DONE/NO_SOURCE/UNKNOWN taxonomy
2. **Create tasks for all genuinely open items**:
   - NO_SOURCE items (pure research, always valuable)
   - DONE items (source experiment completed, question still open)
3. **Use subprocess pattern** for atomic creation:

```python
import subprocess, json

tasks = [
    {"title": "exp_AUTO: <research question>", "assignee": "prometheus-worker-N", "body": "..."},
    # ... more tasks
]

for task in tasks:
    cmd = ["hermes", "kanban", "create", task["title"], "--assignee", task["assignee"], "--body", task["body"]]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        print(f"Created: {task['title'][:60]}")
```

## Prevention

Include this rule in the Director workflow: "if `batch_create_tasks.py` creates < 50% of available free worker slots, create additional tasks manually."

## Diversity Cap Interaction (Cycle #245, 2026-06-02)

Even with `--min-score 30` (21 items qualifying, 7 uncovered), the 35% thread diversity cap limited output to 4 tasks when 6 workers were free. The cap works per-thread: `floor(0.35 × N)` where N is total created tasks. With 2 threads at max 2/task = 4 max. Lowering min-score does NOT bypass the diversity cap — it only adds more qualifying items that still hit the cap.

**Workaround**: After running the script, check `Created N/M tasks`. If N < 50% of free workers, create remaining tasks manually. The script's `--count` parameter should match free worker count, but diversity cap may still limit output.

## --min-score Threshold Impact (Director Pass #603, 2026-06-03)

The `--min-score` parameter directly controls how many queue items qualify for task creation. The default threshold of 60 is often too high, filtering out genuinely valuable research questions.

### Observed Behavior

| Threshold | Items qualifying | Tasks created | Workers filled |
|-----------|-----------------|---------------|----------------|
| `--min-score 60` | 6 | 2 | 2/12 |
| `--min-score 30` | 23 | 10 | 10/12 |

The difference: items scoring 30–59 are valid research questions that the diversity cap and coverage scoring would still filter if they were duplicates or already covered. The min-score is a pre-filter — setting it too high removes genuinely open questions before the more sophisticated deduplication even runs.

### Rule

**Always use `--min-score 30` when filling free worker slots.** The 60-point threshold was designed for a smaller queue (20–30 items). With 40+ queue items and 10+ free workers, 30 produces the right fill rate. Only raise to 60 when queue is small (<20 items) and you want to be selective.

### Detection

After `batch_create_tasks.py --dry-run`, if `Created N` where N < 50% of free workers AND the queue has ≥20 items, lower `--min-score` to 30 and re-run. The existing "batch script conservative scoring" pitfall covers Jaccard overlap issues; this is the simpler threshold-level cause.

## Verified Fix (Cycle #248)

- Batch script: 1 task created
- Manual creation: 11 tasks created
- Total: 12 tasks (filled all free workers)
- All dispatched successfully

## Coverage Thresholds Too Aggressive (Cycle #601, 2026-06-03)

The batch creator's Jaccard overlap thresholds for marking items as "covered" were too aggressive, causing valid follow-up experiments to be filtered out.

### Problem

In Cycle #601, the batch creator reported `Uncovered by running tasks: 0` even though:
- 5 high-value queue items (score >= 60) existed
- 13 workers were free
- All 5 items had high hypothesis overlap (>0.4) with completed experiments

### Root Cause

The coverage logic checks:
1. Hypothesis overlap > 0.4 with any completed experiment
2. Result overlap > 0.65 with any completed experiment

These thresholds are too low for word-overlap-based deduplication. Follow-up experiments naturally share significant word overlap with their source experiments because they're investigating variations of the same research question.

### Example

Queue item: `[NEW from synthesis v578] Isotonic calibration scaling law N^{-0.316} (exp_753)`
Completed experiment: `exp_2057` with hypothesis `[NEW from synthesis v578] Isotonic calibration scaling law N^{-0.316} (exp_753)`

Hypothesis overlap: 0.636 (> 0.4 threshold)
Result: Item marked as "covered" and skipped

But the queue item is a follow-up question, not a duplicate.

### Fix Applied (Cycle #601)

Increased thresholds in `batch_create_tasks.py`:
- Hypothesis overlap: 0.4 → 0.6
- Result overlap: 0.65 → 0.75

This allows legitimate follow-up experiments while still catching true duplicates.

### Detection

After running `batch_create_tasks.py --dry-run`:
1. Check `Uncovered by running tasks: N`
2. If N = 0 AND you have ≥3 free workers AND queue has ≥10 items
3. Run manual classification to verify the script isn't being too aggressive
4. Check hypothesis overlap for high-value items: if many have overlap >0.4 but <0.6, the old thresholds were filtering them out

### Prevention

The Director should:
1. Always run `batch_create_tasks.py --dry-run` first
2. If it produces 0 tasks but free workers exist, verify the coverage logic
3. Consider manually creating tasks for high-score items that are "covered" but not truly answered
4. Monitor the batch creator's behavior after threshold changes to ensure it's not too permissive

## Topic-Level Overlap with Running Tasks (Director Pass #610, 2026-06-03)

The batch creator's "Uncovered by running tasks" count filters items whose **source experiment ID** is running — but it does NOT filter items whose **topic** overlaps with running tasks. This produces false candidates: items that are "uncovered" by ID but duplicate work already in flight.

### Problem

With 19 running tasks and 3 free workers, `--min-score 30` produced:
```
Queue: 23 active items, 23 score >= 30
Uncovered by running tasks: 15
Selected: 3 tasks

DRY RUN — tasks that would be created:
   1. [ 60] [lr_detection] → worker-7: Direct LR 36x faster but training 4.4x longer
   2. [ 54] [injection   ] → worker-15: C=50.0 optimal across ALL real injection types
   3. [ 52] [other       ] → worker-20: EU AI Act deference 20% drop
```

Items 2 and 3 overlap with running tasks:
- "C=50.0 optimal" → exp_2230 AND exp_2235 already running
- "EU AI Act deference" → exp_2202, exp_2223, exp_2232, exp_2236 already running

Both topics have 2+ active experiments. Creating duplicates wastes worker slots.

### Root Cause

The coverage check in `batch_create_tasks.py` filters by source experiment ID (`exp_NNN` in queue text vs running task titles). Items without source IDs (NO_SOURCE) are always "uncovered." The Jaccard keyword overlap check runs against running titles, but the diversity cap and scoring can still select items with moderate overlap.

### Detection

After `batch_create_tasks.py --dry-run` produces candidates, cross-reference each candidate's core topic against running task titles. If the same research question already has ≥1 running task, skip it — the running experiment will resolve the queue item when it completes and synthesis processes it.

### Manual Override Pattern

When the batch creator selects overlapping items:
1. Read the dry-run output
2. For each candidate, check if its core topic (not just keywords) is already being investigated
3. Replace overlapping candidates with genuinely distinct high-value items from the uncovered list
4. Create tasks manually using `hermes kanban create`

```python
# Quick topic overlap check
import json
running = json.load(open('/tmp/kanban_running.json'))
running_titles = ' '.join(t.get('title', '').lower() for t in running)

# Check if a candidate topic overlaps
candidate = "C=50.0 optimal across ALL real injection types"
core_phrases = ['C=50', 'injection type']
overlaps = any(phrase.lower() in running_titles for phrase in core_phrases)
# overlaps = True → skip this candidate
```

### Rule

**After batch creator dry-run, always manually verify that selected items don't duplicate running experiment topics.** The script's "Uncovered by running tasks" count is necessary but not sufficient — it checks ID-level coverage, not topic-level coverage. Items with NO_SOURCE are always "uncovered" by ID, even when their topic is actively being investigated.

## Script Crash on Integer experiments Field (2026-06-03)

`batch_create_tasks.py` can crash with `TypeError: 'int' object is not subscriptable` or `AttributeError: 'int' object has no attribute 'get'` when self_state.json has `experiments` as an integer count instead of a dict. The script assumes `state['experiments']` is a dict with `running`/`completed` keys.

**Fix**: The script should check `isinstance(state.get('experiments', {}), dict)` before accessing sub-keys. When it's an int, use `metrics.experiments_completed_list` for completed experiments and skip running (not tracked in self_state).

**Detection**: Script output shows `ERROR: Could not score queue: 'int' object has no attribute 'get'`. This is the same schema variant issue documented in `director-loop` skill `references/unsynth-detection-regex-false-positive.md`.

## Batch Creator Timeout on Large Queues (Cycle #246, June 2026)

The `batch_create_tasks.py` script can time out after 60 seconds when the queue is large (283 items, 219 qualifying). The timeout occurs during the scoring computation phase, not during task creation.

### Problem

With 283 active queue items and 219 scoring ≥60, the batch creator's scoring function (which computes Jaccard overlap against running titles, completed experiments, and intra-batch items) exceeds the 60-second terminal timeout. The command returns exit code 124 (timeout) with no output.

### Detection

Terminal output shows `[Command timed out after 60s]` with exit code 124. No tasks are created.

### Workaround

1. **Reduce queue size first**: Run `queue_cleanup.py --apply` to deduplicate and cap at 50 items before running batch creator
2. **Lower --min-score**: Using `--min-score 80` instead of 60 reduces the candidate set dramatically (from 219 to ~10-20 items), avoiding the timeout
3. **Split into smaller batches**: Run with `--count 10` instead of `--count 50` — smaller candidate sets score faster
4. **Manual fallback**: When timeout occurs, manually create tasks for the highest-scoring items using `hermes kanban create`

### Root Cause

The scoring function computes pairwise Jaccard overlap between each queue item and: (a) all running task titles, (b) all completed experiment hypotheses/results from prometheus.db, (c) all previously selected items in the current batch. With 283 items × ~40 running tasks × ~500 completed experiments, the quadratic scaling exceeds 60s.

### Prevention

Before running batch creator on a large queue:
1. Check queue size: `python3 -c "import json; ss=json.load(open('~/.hermes/self_state.json')); print(len(ss.get('curiosity_queue',[])))"`
2. If queue > 100 items, run cleanup first OR use `--min-score 80` to reduce candidate set
3. If queue > 200 items, the batch creator will likely timeout — use manual creation or split into multiple passes with `--count 10`

## Thread Distribution Skew with [TRANSFER] Items (Cycle #246, June 2026)

The 35% per-thread diversity cap fails to prevent thread monopolization when the queue is heavily skewed toward [TRANSFER]-prefixed items. The `classify_thread()` function strips the `[TRANSFER]` prefix before classifying by topic, but most queue items' underlying topics still map to "cross_pollination" because they're genuinely cross-domain questions.

### Observed Behavior

In Cycle #246, running task thread distribution was:
- cross_pollination: 30 (83%)
- other: 6 (17%)
- attack: 0, embedding: 0, tfidf: 0, injection: 0

The diversity cap (35% = max 5 tasks per thread with --count 15) was not the bottleneck — the queue itself was 80%+ cross_pollination items. The cap merely prevented even worse monopolization.

### Impact

All 50 workers investigate the same thread (cross-domain transfer questions), producing diminishing returns. The system needs more variety in the queue itself, not just in task creation.

### Detection

After batch creation, check thread distribution in output. If any single thread exceeds 60% of created tasks, the queue is skewed.

### Fix

1. **Queue-level intervention**: Manually add non-[TRANSFER] items to the queue from experiment results (e.g., "Does X hold for Y?" instead of "[TRANSFER] Can X transfer to Y?")
2. **Thread-aware scoring**: The curiosity scorer should boost non-cross_pollination items when cross_pollination dominates the queue
3. **Manual thread diversification**: After batch creation, replace some cross_pollination tasks with manually created tasks from underrepresented threads (attack, embedding, tfidf, injection)

### Prevention

The Director should monitor thread distribution mid-pass. If cross_pollination exceeds 60% of running tasks, create tasks for underrepresented threads even if their queue items score lower. Worker capacity is wasted when all workers investigate the same question from different angles.
