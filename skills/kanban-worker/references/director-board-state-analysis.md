# Director Board-State Analysis — Reusable Pattern

Consolidated script for the full Director board-state analysis: dump board, detect unsynthesized experiments, classify queue items, compute coverage scores, and identify mid-pass completions. Combines TIRITH-safe file I/O, queue classification taxonomy, and coverage scoring from multiple kanban-worker pitfalls.

## Usage

1. Dump board state (two-step, TIRITH-safe):
```bash
hermes kanban list --status running --json > /tmp/kanban_running.json
hermes kanban list --status done --json > /tmp/kanban_done.json
```

2. Run the analysis script (write to file, then execute — avoids TIRITH pipe blocks):
```python
# Save as /tmp/board_analysis.py, then run: python3 /tmp/board_analysis.py
```

## Full Script

```python
import json, re, os
from collections import defaultdict

# --- BOARD STATE ---
running = json.load(open('/tmp/kanban_running.json'))
done = json.load(open('/tmp/kanban_done.json'))

print(f"Running tasks: {len(running)}")
for t in running:
    print(f"  {t['id']}: {t.get('title','?')[:90]}")

print(f"\nDone tasks: {len(done)}")

# --- UNSYNTHESIZED DETECTION (Method A) ---
done_exp_ids = set()
for t in done:
    title = t.get('title', '')
    is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
    if not is_synth:
        for m in re.finditer(r'exp_(\d+\\w*)', title):
            done_exp_ids.add(f'exp_{m.group(1)}')

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else ''
    if not eid and isinstance(e, str):
        m = re.search(r'exp_(\\d+\\w*)', e)
        if m: eid = f'exp_{m.group(1)}'
    if eid: ss_exp_ids.add(eid)

unsynth = done_exp_ids - ss_exp_ids
print(f"\\nDone exp IDs: {len(done_exp_ids)}, Self-state: {len(ss_exp_ids)}, Unsynthesized: {len(unsynth)}")
for eid in sorted(unsynth, key=lambda x: int(re.search(r'(\\d+)', x).group(1)) if re.search(r'(\\d+)', x) else 0):
    print(f"  {eid}")

# --- RUNNING EXP IDS ---
running_exp_ids = set()
for t in running:
    for m in re.finditer(r'exp_(\\d+\\w*)', t.get('title', '')):
        running_exp_ids.add(f'exp_{m.group(1)}')

# --- QUEUE CLASSIFICATION ---
queue = ss.get('curiosity_queue', [])
resolved_count = sum(1 for item in queue if 'RESOLVED' in (item.get('text', str(item)) if isinstance(item, dict) else str(item)))
active_count = len(queue) - resolved_count
print(f"\\nQueue: {len(queue)} items ({resolved_count} RESOLVED, {active_count} ACTIVE)")

# --- COVERAGE SCORING ---
stop_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
              'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
              'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
              'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
              'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
              'which', 'who', 'we', 'you', 'they', 'new', 'also', 'into', 'more'}
running_titles_text = ' '.join(t.get('title', '').lower() for t in running)

print("\\nActive queue items:")
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue

    # Source classification
    source_ids = re.findall(r'exp_(\\d+\\w*)', text)
    if not source_ids:
        cat = "NO_SOURCE"
    elif any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
        cat = "RUNNING"
    elif any(f'exp_{sid}' in ss_exp_ids for sid in source_ids):
        cat = "DONE"
    else:
        cat = "UNKNOWN"

    # Coverage scoring
    words = set(w.lower() for w in text.split() if len(w) > 3)
    keywords = {w for w in (words - stop_words) if not re.match(r'^exp_\\d+\\w*$', w)}
    overlap = sum(1 for w in keywords if w in running_titles_text)
    coverage = overlap / max(len(keywords), 1)
    cov_label = "HIGH" if coverage > 0.3 else ("MED" if coverage > 0.15 else "LOW")

    print(f"  [{i}] ({cat}, cov={cov_label} {coverage:.2f}) {text[:100]}")

# --- WORKER AVAILABILITY ---
import subprocess
result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
busy_workers = set()
for line in result.stdout.split('\\n'):
    m = re.search(r'prometheus-worker-(\\d+)', line)
    if m:
        busy_workers.add(int(m.group(1)))
all_workers = set(range(1, 23))
free_workers = all_workers - busy_workers
print(f"\\nWorkers: {len(busy_workers)} busy, {len(free_workers)} free")
if free_workers:
    print(f"  Free: {sorted(free_workers)}")
```

## Key Patterns Used

1. **TIRITH-safe file I/O**: Write JSON to `/tmp/` files, process in separate commands. Never pipe CLI output to python3.
2. **Queue classification taxonomy**: RESOLVED → RUNNING → DONE → NO_SOURCE → UNKNOWN. Only create tasks for NO_SOURCE (low coverage) and DONE (question unanswered).
3. **Coverage scoring**: Keyword overlap between queue item text and running task titles. HIGH (>0.3) = covered, MED (0.15-0.3) = partial, LOW (<0.15) = open.
4. **Worker availability**: `ps aux | grep prometheus-worker-N` for ground truth. `kanban list` doesn't include assignee field.
5. **Mid-pass completion detection**: Re-check running list after task creation — experiments may complete during the pass. Add to synthesis via `kanban_comment` before synthesis worker starts.

## Mid-Pass Completion Pattern

When experiments complete between board dump and synthesis task dispatch:
1. Re-check `kanban list --status running` after creating tasks
2. Read summaries of newly completed experiments via `kanban show --json`
3. Add to synthesis task via `kanban_comment` with experiment details
4. Tag corresponding queue items as RESOLVED in the comment

This prevents the "synthesis misses completed experiments" race condition (see kanban-worker pitfall: comment race condition).
