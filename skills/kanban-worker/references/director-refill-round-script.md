# Director Refill Round Script (Cycle #280+)

**Purpose:** Fill free workers when `batch_create_tasks.py` returns too few items.
**Trigger:** Batch creator finds < 50% of free worker slots uncovered.
**Validated:** Cycle #280 — created 66 tasks across 4 refill rounds.

## The Problem

The batch creator's coverage check is too broad — it reports "0 uncovered" while 20+ workers sit idle. The manual fallback script below uses stricter word overlap (< 0.4) to find genuinely uncovered items.

## Working Script

Write to `/tmp/create_tasks_batch.py` and execute:

```python
import json, sqlite3, re, subprocess as sp

# --- Configuration ---
KANBAN_DB = '~/.hermes/kanban.db'
SELF_STATE = '~/.hermes/self_state.json'
MIN_OVERLAP = 0.4  # Items below this threshold are "uncovered"
SKIP_KEYWORDS = ['RESOLVED', 'ALREADY ANSWERED', 'ALREADY_ANSWERED']

# --- Load queue ---
state = json.load(open(SELF_STATE))
queue = state.get('curiosity_queue', [])

# --- Get running task titles ---
conn = sqlite3.connect(KANBAN_DB)
running = [r[0] for r in conn.execute(
    "SELECT title FROM tasks WHERE status='running'"
).fetchall()]

# --- Get next experiment ID ---
r = conn.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
ids = [int(re.search(r'exp_(\d+)', x[0]).group(1))
       for x in r if re.search(r'exp_(\d+)', x[0])]
next_id = max(ids) + 1 if ids else 1
conn.close()

# --- Detect free workers via process list ---
result = sp.run(['ps', 'aux'], capture_output=True, text=True)
busy = set()
for line in result.stdout.splitlines():
    if 'prometheus-worker' in line and 'hermes' in line:
        m = re.search(r'-p\s+(prometheus-worker-\d+)', line)
        if m:
            busy.add(m.group(1))
all_w = set([f'prometheus-worker-{i}' for i in range(1, 51)])
free_workers = sorted(all_w - busy, key=lambda x: int(x.split('-')[-1]))

# --- Word overlap function ---
def word_overlap(a, b):
    words_a = set(re.findall(r'\w+', a.lower()))
    words_b = set(re.findall(r'\w+', b.lower()))
    if not words_a or not words_b:
        return 0
    intersection = words_a.intersection(words_b)
    union = words_a.union(words_b)
    return len(intersection) / len(union)

# --- Find uncovered items ---
uncovered = []
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if any(kw in text.upper() for kw in SKIP_KEYWORDS):
        continue
    max_overlap = max((word_overlap(text, rt) for rt in running), default=0)
    if max_overlap < MIN_OVERLAP:
        uncovered.append((i, max_overlap, text))

print(f"Free workers: {len(free_workers)}")
print(f"Uncovered items: {len(uncovered)}")

# --- Create tasks ---
created = 0
for i, (idx, score, text) in enumerate(uncovered[:len(free_workers)]):
    worker = free_workers[i]
    exp_id = next_id + i
    title = f"exp_{exp_id}: {text[:70]}"

    # Rich body matching experiment-task-body-template.md format
    body = f"""CONTEXT: Queue item #{idx} from refill round. Score: {score:.2f}. No prior experiment covers this specific hypothesis.

HYPOTHESIS: {text}

MANDATORY — RAG CHECK (do this FIRST, before writing any code):
  python3 ~/.hermes/scripts/experiment_rag.py query "{text[:80]}" --top-k 5 --worker-id worker

  After running the query, check the results:
  - Score > 0.7: READ the top result. If already answered, report instead of duplicating.
  - Score 0.4-0.7: Check overlap. Build on it if relevant.
  - Score < 0.4 or server down: proceed with your own approach.

METHOD:
1. Design a concrete experiment to test this hypothesis with measurable outcomes
2. Implement using standard ML tools (sklearn, numpy, etc.) or domain-appropriate methods
3. Run experiment and collect quantitative results
4. Analyze results with appropriate statistical tests

EXPECTED: State specific thresholds that would confirm or refute the hypothesis before running.

DEFAULT MODEL: mimo-v2.5 via OpenRouter. Workers have FULL terminal access for local scripts — HTTP calls. Workers NEVER SSH to servers.
GPU: Workers can use local RTX 5090 via gpu_run CLI for experiments that benefit from local inference.

RESULT WRITING (MANDATORY — do this BEFORE kanban_complete):
python3 ~/.hermes/scripts/write_worker_result.py \\
  --experiment exp_{exp_id} \\
  --finding "WHAT you found AND WHY it works (mechanism)" \\
  --supported \\
  --confidence 0.85 \\
  --domain <domain> \\
  --tags CONFIRMED,surprise \\
  --files "exp_{exp_id}.py,exp_{exp_id}_results.json" \\
  --queue "Follow-up question?;[TRANSFER] Cross-domain question?" \\
  --predicted-direction '{{"your_prediction": 1}}' \\
  --observed-direction '{{"your_observation": 1}}' \\
  --design-vector '{{"instrument": "API", "distribution": "mixed", "metric": "accuracy"}}'
"""

    cmd = ['hermes', 'kanban', 'create', title, '--assignee', worker, '--body', body]
    result = sp.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        m = re.search(r'(t_[a-f0-9]+)', result.stdout)
        task_id = m.group(1) if m else 'unknown'
        print(f"Created: exp_{exp_id} -> {worker} (task {task_id})")
        created += 1
    else:
        print(f"FAILED: exp_{exp_id} -> {worker}: {result.stderr[:100]}")

print(f"\nTotal created: {created}")
```

## Refill Round Pattern

Workers complete in 1–12 minutes. A pass that creates 22 tasks may see 10+ complete within 5 minutes. This means:

1. Create batch → dispatch → wait 60s
2. Re-check free workers → create another batch
3. Repeat until queue is saturated (all items covered by running tasks)
4. Expect 3–4 refill rounds per Director pass at 50 workers

## When to Stop

- Queue items all have overlap >= 0.4 with running tasks (saturated)
- Fewer than 3 free workers remain
- Queue size > 50 (run cleanup first)

## Key Differences from batch_create_tasks.py

| Aspect | batch_create_tasks.py | This script |
|--------|----------------------|-------------|
| Coverage threshold | 0.45-0.65 (configurable) | 0.4 (fixed) |
| Dedup layers | 3 (Jaccard + phrase + queue) | 1 (word overlap only) |
| Worker detection | Profile-based | Process list (ps aux) |
| Speed | Slower (scoring + dedup) | Faster (simple overlap) |
| False negatives | Common at high thresholds | Rare at 0.4 threshold |

Use this script when batch_create_returns < 50% of free worker slots.
