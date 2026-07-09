# Director: Determining Worker Availability

## JSON Format (Corrected June 2026)

`hermes kanban list --status running --json` returns a **plain JSON array** of task objects, NOT a dict with a `"tasks"` key. Each object includes `id`, `title`, `body`, `status`, and **`assignee`** (e.g., `"prometheus-worker-5"`).

```python
# CORRECT parsing:
import json
with open('running.json') as f:
    tasks = json.load(f)  # This is a list, not {"tasks": [...]}

# WRONG — will crash with AttributeError:
# tasks = json.load(f).get('tasks', [])
```

## Free Worker Detection (Two-Step)

**Step 1:** Extract busy profiles from running task assignees:
```python
import re, json
busy_profiles = set()
for t in running_tasks:
    m = re.search(r'prometheus-worker-(\d+)', t.get('assignee', ''))
    if m:
        busy_profiles.add(int(m.group(1)))
```

**Step 2:** Get active worker processes via `ps aux`:
```python
import subprocess
ps_result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=10)
active_workers = set()
for line in ps_result.stdout.split('\n'):
    m = re.search(r'prometheus-worker-(\d+)', line)
    if m:
        active_workers.add(int(m.group(1)))

free_workers = active_workers - busy_profiles
```

**Why both steps?** `ps aux` shows who's alive. `assignee` shows who has a task. A worker can be alive without a task (free), or have a task without being alive (stuck). You need both signals.

## Stuck Task Detection

Tasks assigned to profiles with no active process are stuck:
```python
stuck = []
for t in running_tasks:
    m = re.search(r'prometheus-worker-(\d+)', t.get('assignee', ''))
    if m and int(m.group(1)) not in active_workers:
        stuck.append(t)
```

## TIRITH: Pipe-to-Interpreter Blocking

`hermes kanban list --json | python3 -c "..."` is blocked by TIRITH's `pipe_to_interpreter` pattern. Workaround: write JSON to a temp file, then parse it in a separate step:
```bash
hermes kanban list --status running --json > /tmp/running.json
python3 -c "import json; tasks=json.load(open('/tmp/running.json'))"
```

## Pitfall: DB Query for Worker Enumeration is Fragile (June 2026)

**Don't do this:**
```python
workers = db.execute("SELECT DISTINCT assignee FROM tasks WHERE assignee LIKE 'prometheus-worker-%'").fetchall()
```

This only finds profiles that have been USED in past tasks. New profiles
(e.g., prometheus-worker-37 through prometheus-worker-10) are invisible
until they get their first task. The free worker count will be wrong.

**Use `ps aux` instead** (see Step 2 above) — it finds all alive worker
processes regardless of past task history. Or use a known worker range:
```python
all_workers = {f'prometheus-worker-{i}' for i in range(1, 51)}
```

**Detection:** Free worker count seems low relative to expected pool size,
or `hermes kanban dispatch` spawns more tasks than predicted.

## When to Check

- BEFORE creating new Kanban tasks (to avoid double-assignment)
- During Director synthesis (to verify worker liveness before deciding to create vs synthesize)
- When diagnosing stuck workers (to confirm process is alive)
- Mid-pass re-check (workers complete fast — re-check free count after each batch creation round)
