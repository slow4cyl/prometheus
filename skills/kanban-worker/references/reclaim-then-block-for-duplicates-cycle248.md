# Reclaim-Then-Block Pattern for Bulk Duplicate Cleanup (Cycle #248)

## Problem
The batch creator can produce massive duplication when queue items share high keyword overlap but have different experiment IDs. In Cycle #248, this resulted in:
- 5× TF-IDF+LR at 100+ domains (exp_2476, 2489, 2530, 2539, 2546)
- 5× C=50.0 optimal across injection types (exp_2477, 2490, 2531, 2540, 2547)
- 2× ensemble disagreement AUC (exp_2532, 2541)
- 2× direct LR speed vs training cost (exp_2542, 2548)

All running simultaneously, consuming 14 of 21 worker slots on duplicate work.

## Three-Step Pattern

### Step 1: Identify orphaned tasks (running but no worker process)

```python
import subprocess, re, json

result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
worker_tasks = {}  # task_id -> worker_num
for line in result.stdout.split('\n'):
    if 'prometheus-worker' in line and 'grep' not in line:
        m_worker = re.search(r'prometheus-worker-(\d+)', line)
        m_task = re.search(r'kanban task (t_\w+)', line)
        if m_worker and m_task:
            worker_tasks[m_task.group(1)] = int(m_worker.group(1))

running = json.load(open('/tmp/kanban_running.json'))
orphaned = [t['id'] for t in running if t['id'] not in worker_tasks]
print(f"Orphaned: {len(orphaned)} / {len(running)} running")
```

### Step 2: Reclaim all orphans

Releasing the worker claim puts the task back to `ready` status.

```bash
for tid in t_325da0a7 t_70d012ac t_a42cddb8 t_b7f27a5c t_4776d9ff t_5d14d63b t_1367315e; do
    hermes kanban reclaim "$tid"
done
```

**Critical**: reclaim ONLY works on `running` tasks. If a task is already `done` or `blocked`, reclaim silently does nothing.

### Step 3: Immediately block duplicates (before next dispatch tick)

Keep the OLDEST running task per topic, block the rest. The block prevents re-dispatch on the next scheduler tick (~1 minute).

```bash
# TF-IDF+LR duplicates — keep t_57efdead (exp_2476, oldest)
hermes kanban block t_325da0a7 "duplicate: same topic as exp_2476 (TF-IDF+LR at 100+ domains) — already running on worker-9"

# C=50.0 duplicates — keep t_bb8bdc13 (exp_2477, oldest)
hermes kanban block t_70d012ac "duplicate: same topic as exp_2477 (C=50.0 optimal) — already running on worker-13"
hermes kanban block t_b7f27a5c "duplicate: same topic as exp_2477 (C=50.0 optimal) — already running on worker-13"
hermes kanban block t_4776d9ff "duplicate: same topic as exp_2477 (C=50.0 optimal) — already running on worker-13"

# Direct LR duplicate — keep t_42005f7a (exp_2542, oldest)
hermes kanban block t_5d14d63b "duplicate: same topic as exp_2542 (Direct LR speed) — already running on worker-17"

# Orphan with 0 workspace files — likely failed to start
hermes kanban block t_a42cddb8 "orphan: no worker process, experiment exp_2528 (Non-Latin ratio features)"

# Synthesis task with 0 workspace files
hermes kanban block t_1367315e "orphan: synthesis task for exp_2448 had 0 workspace files, likely failed to start"
```

## Why Reclaim Before Block

- `reclaim` only works on `running` tasks (releases the worker claim)
- `block` works on any non-done task (prevents re-dispatch)
- Order matters: if you block first, reclaim fails silently (task is no longer running)
- If you reclaim but don't block, the task goes to `ready` and gets re-dispatched

## Timing

The dispatch tick runs every ~1 minute. Between reclaim and block, the task is in `ready` status and could be dispatched. Block must happen within the same pass, before the next dispatch tick. In practice, executing reclaim→block in sequence within the same Director pass is fast enough.

## Result (Cycle #248)

- 7 orphaned tasks reclaimed
- 7 duplicate/orphan tasks blocked
- 3 free workers created
- 3 targeted new tasks created for genuinely uncovered queue items
- Board stabilized from 28 running/21 workers to 21 running/21 workers

## Keyword Overlap Coverage Scoring

Before creating new tasks, score NO_SOURCE queue items against running task titles:

```python
stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'does'}
running_text = ' '.join(t.get('title', '').lower() for t in running)

for item in queue_items:
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    words = set(re.findall(r'\b[a-z]{3,}\b', text.lower()))
    keywords = {w for w in words - stop if not re.match(r'^exp_\d+$', w)}
    overlap = sum(1 for w in keywords if w in running_text)
    coverage = overlap / max(len(keywords), 1)
    # LOW <0.3 = genuinely uncovered → create task
    # MED 0.3-0.5 = partially covered → lower priority
    # HIGH >0.5 = likely covered → skip
```

In Cycle #248, this identified 11 genuinely uncovered items out of 39 NO_SOURCE candidates. The top 3 (coverage <0.15) were selected for task creation:
1. Domain-aware confidence gating (cov=0.13)
2. Lightweight precision-boosting post-filter (cov=0.13)
3. Domain anchor semantic analysis (cov=0.15)
