#!/usr/bin/env python3
"""
Manual task creation fallback for Director passes.

Use when batch_create_tasks.py crashes (e.g., experiments field is integer
instead of dict, or queue items are mixed types). This script:
1. Parses queue items (handles both JSON string and dict formats)
2. Loads running task titles from kanban list
3. Filters uncovered items by word overlap threshold
4. Creates experiment tasks with RAG + GPU instructions in body
5. Assigns to free workers (detected via ps aux)

Usage:
  python3 ~/.hermes/skills/kanban-worker/references/manual-task-creation-script.py

Or adapt the functions for inline use in a Director pass.
"""
import json, re, subprocess, sys, os, sqlite3

def get_next_experiment_id():
    """Get next experiment ID from kanban.db."""
    c = sqlite3.connect(os.path.expanduser('~/.hermes/kanban.db'))
    r = c.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
    ids = [int(re.search(r'exp_(\d+)', x[0]).group(1)) for x in r if re.search(r'exp_(\d+)', x[0])]
    c.close()
    return max(ids) + 1 if ids else 1

def parse_queue(ss):
    """Parse curiosity queue, handling both string and dict formats."""
    queue = ss.get('curiosity_queue', [])
    parsed = []
    for item in queue:
        if isinstance(item, str):
            try:
                parsed.append(json.loads(item))
            except:
                parsed.append({"text": item, "source": "unknown"})
        elif isinstance(item, dict):
            parsed.append(item)
    return parsed

def get_running_titles(running_file='/tmp/kanban_running.json'):
    """Load running task titles."""
    running = json.load(open(running_file))
    if isinstance(running, dict):
        running = running.get('tasks', [])
    return [t.get('title', '').lower() for t in running]

def word_overlap(text, titles, threshold=0.3):
    """Check if text overlaps with any running title."""
    words = set(re.findall(r'\w+', text.lower()))
    if not words:
        return False
    for title in titles:
        title_words = set(re.findall(r'\w+', title))
        if not title_words:
            continue
        overlap = len(words & title_words) / min(len(words), len(title_words))
        if overlap > threshold:
            return True
    return False

def get_free_workers():
    """Get list of free worker numbers from ps aux."""
    result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
    busy = set()
    for line in result.stdout.split('\n'):
        m = re.search(r'prometheus-worker-(\d+)', line)
        if m:
            busy.add(int(m.group(1)))
    all_workers = set(range(1, 51))
    return sorted(all_workers - busy)

def create_experiment_tasks(ss, running_titles, free_workers, max_tasks=20):
    """Create experiment tasks for free workers."""
    parsed = parse_queue(ss)
    uncovered = [item for item in parsed
                 if not word_overlap(item.get('text', ''), running_titles)]

    n_tasks = min(len(free_workers), len(uncovered), max_tasks)
    next_id = get_next_experiment_id()

    task_body_template = """RESEARCH EXPERIMENT

Hypothesis: {hypothesis}

Source: {source}

INSTRUCTIONS:
1. Search experiment RAG first: python3 ~/.hermes/scripts/experiment_rag.py query "{keywords}" --top-k 3 --worker-id worker
2. If a highly relevant past experiment exists (score > 0.7), build on it instead of starting fresh
3. Design and run an experiment to test this hypothesis
4. Write results to your workspace
5. MANDATORY: Before completing, call write_worker_result.py:
   python3 ~/.hermes/scripts/write_worker_result.py --experiment exp_{exp_id} --finding "CONFIRMED: F1=0.95 BECAUSE adversarial inputs cluster in low-dim subspace" --supported \
                            # ONLY if finding starts with CONFIRMED/SUPPORTED
                            # Use --refuted if finding starts with REFUTED
                            # The finding text is the source of truth — flag must match it
                            --confidence 0.85 --domain injection_detection --type MECHANISTIC --tags TAGS

DEFAULT MODEL: mimo-v2.5 via OpenRouter. Workers have FULL terminal access for local scripts.
GPU: Workers can use local RTX 5090 via gpu_run CLI for experiments that benefit from local inference.

Write all output to $HERMES_KANBAN_WORKSPACE (NOT /tmp or ~/).
"""

    created = []
    for i in range(n_tasks):
        item = uncovered[i]
        text = item.get('text', '')
        source = item.get('source', 'unknown')
        exp_id = next_id + i
        keywords = ' '.join(re.findall(r'\w+', text)[:5])

        body = task_body_template.format(
            hypothesis=text, source=source,
            keywords=keywords, exp_id=exp_id,
        )

        title = f"exp_{exp_id}: {text[:80]}"
        worker = free_workers[i]
        assignee = f"prometheus-worker-{worker}"

        cmd = ["hermes", "kanban", "create", title, "--assignee", assignee, "--body", body]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            task_id_match = re.search(r'(t_[a-f0-9]+)', result.stdout)
            task_id = task_id_match.group(1) if task_id_match else f"exp_{exp_id}"
            created.append(task_id)
            print(f"  Created: exp_{exp_id} -> {assignee} ({task_id})")
        else:
            print(f"  FAILED: exp_{exp_id} -> {assignee}: {result.stderr[:100]}")

    return created

if __name__ == '__main__':
    # Load self_state
    with open(os.path.expanduser('~/.hermes/self_state.json')) as f:
        ss = json.load(f)

    running_titles = get_running_titles()
    free_workers = get_free_workers()

    print(f"Free workers: {len(free_workers)}")
    created = create_experiment_tasks(ss, running_titles, free_workers)
    print(f"\nTotal created: {len(created)}")
