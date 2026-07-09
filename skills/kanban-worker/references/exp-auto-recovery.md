# exp_AUTO Recovery — Renaming and Recovering Experiment Data

When batch_create_tasks.py used `exp_AUTO:` as the title prefix (June 2026), completed experiments were invisible to the tracking pipeline. This document covers recovery.

## Why exp_AUTO Breaks the Pipeline

The sync pipeline extracts experiment IDs from task titles via regex: `re.search(r'exp_(\d+)', title)`. Titles starting with `exp_AUTO:` produce no match → sync skips the task → experiment never appears in SQLite → dashboard count doesn't climb.

## Recovery: Completed exp_AUTO Tasks

For each completed exp_AUTO task, extract data and insert into SQLite:

```python
import sqlite3, re, json, subprocess
from datetime import datetime, timezone

# 1. Get all exp_AUTO done tasks
r = subprocess.run(['hermes', 'kanban', 'list', '--status', 'done', '--json'],
                   capture_output=True, text=True, timeout=30)
tasks = json.loads(r.stdout)
auto_tasks = [t for t in tasks if 'exp_AUTO' in t.get('title', '')]

# 2. Get next experiment ID from SQLite
conn = sqlite3.connect('~/.hermes/prometheus.db')
existing = set(r[0] for r in conn.execute("SELECT id FROM experiments").fetchall())
next_id = max([int(re.search(r'(\d+)', x).group(1)) for x in existing if re.search(r'(\d+)', x)]) + 1

# 3. For each task, extract data and insert
for task in auto_tasks:
    # Get run summary via kanban show
    r2 = subprocess.run(['hermes', 'kanban', 'show', task['id'], '--json'],
                        capture_output=True, text=True, timeout=15)
    detail = json.loads(r2['output'])
    
    # Extract hypothesis from title (after "exp_AUTO: ")
    hypothesis = re.sub(r'^exp_AUTO:\s*', '', task['title']).strip()[:200]
    
    # Extract result from run summary
    result = ""
    for run in detail.get('runs', []):
        if run.get('summary'):
            result = run['summary']
            break
    
    if not result or len(result) < 20:
        continue  # skip tasks with no meaningful result
    
    # Assign new ID and insert
    exp_id = f'exp_{next_id}'
    next_id += 1
    now = datetime.now(timezone.utc).isoformat()
    
    conn.execute(
        """INSERT OR IGNORE INTO experiments
           (id, hypothesis, result, status, confidence_change, tags,
            domain, model, created_at, completed_at)
           VALUES (?, ?, ?, 'completed', 0, ?, '', 'xiaomi/mimo-v2.5', ?, ?)""",
        (exp_id, hypothesis, result, json.dumps([]), now, now)
    )

conn.commit()
conn.close()
```

## Recovery: Running exp_AUTO Tasks

Rename directly in the kanban SQLite database:

```python
import sqlite3, re

conn = sqlite3.connect('~/.hermes/kanban.db')
conn.execute('PRAGMA busy_timeout=5000')

# Get next ID
pconn = sqlite3.connect('~/.hermes/prometheus.db')
existing = set(r[0] for r in pconn.execute("SELECT id FROM experiments").fetchall())
pconn.close()
next_id = max([int(re.search(r'(\d+)', x).group(1)) for x in existing if re.search(r'(\d+)', x)]) + 1

# Find and rename
rows = conn.execute(
    "SELECT id, title FROM tasks WHERE title LIKE '%exp_AUTO%' AND status IN ('running','ready','blocked')"
).fetchall()

for task_id, title in rows:
    desc = re.sub(r'^exp_AUTO:\s*', '', title).strip()
    new_title = f'exp_{next_id}: {desc}'
    conn.execute('UPDATE tasks SET title=? WHERE id=?', (new_title, task_id))
    next_id += 1

conn.commit()
conn.close()
```

## Blocked Queue Cleanup

When the blocked queue has a mix of corrupted, exp_AUTO, and proper-ID tasks:

```python
import sqlite3, re

conn = sqlite3.connect('~/.hermes/kanban.db')
rows = conn.execute('SELECT id, title FROM tasks WHERE status="blocked"').fetchall()

for task_id, title in rows:
    # Corrupted nested JSON → archive
    if "{'text'" in title or '{\\\\' in title:
        conn.execute('UPDATE tasks SET status="done", result="ARCHIVED: corrupted" WHERE id=?', (task_id,))
        continue
    
    # exp_AUTO → rename and unblock
    if 'exp_AUTO' in title:
        desc = re.sub(r'^exp_AUTO:\s*', '', title).strip()
        new_title = f'exp_{next_id}: {desc}'
        conn.execute('UPDATE tasks SET title=?, status="ready" WHERE id=?', (new_title, task_id))
        next_id += 1
        continue
    
    # Already has proper ID → just unblock
    conn.execute('UPDATE tasks SET status="ready" WHERE id=?', (task_id,))

conn.commit()
conn.close()
```

## Prevention

`batch_create_tasks.py` now reads the highest experiment ID from SQLite and generates sequential IDs. The Director prompt includes an instruction to always check SQLite for the next ID when creating tasks manually. Any script that generates task titles for experiment work MUST use the `exp_NNN:` format.
