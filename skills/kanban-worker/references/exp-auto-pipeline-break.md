# exp_AUTO Title Pipeline Break (June 2026)

## Problem

When `batch_create_tasks.py` used `exp_AUTO:` as the task title prefix,
completed experiments could never be tracked in SQLite.

### Why it breaks

The experiment tracking pipeline extracts experiment IDs from task titles via regex:

```python
re.search(r'exp_(\d+)', title)
```

Titles starting with `exp_AUTO:` produce no match → sync skips the task →
experiment never appears in SQLite → dashboard count doesn't climb.

### Impact

- 167 completed experiments were invisible to the dashboard
- Dashboard showed 1,556 experiments while kanban had 2,156 done tasks
- Phases never climbed because no new experiments were being recorded

## Fix

`batch_create_tasks.py` now reads the highest experiment ID from SQLite
and generates sequential IDs (`exp_1673`, `exp_1674`, ...).

## Recovery

### For running exp_AUTO tasks

Rename directly in the kanban SQLite database:

```bash
sqlite3 ~/.hermes/kanban.db "UPDATE tasks SET title = REPLACE(title, 'exp_AUTO', 'exp_NNN') WHERE id = 'task_id'"
```

Or in batch:

```python
import sqlite3, re
conn = sqlite3.connect('~/.hermes/kanban.db')
rows = conn.execute('SELECT id, title FROM tasks WHERE title LIKE "%exp_AUTO%" AND status="running"').fetchall()
next_id = 1773  # next available ID
for task_id, old_title in rows:
    desc = re.sub(r'^exp_AUTO:\s*', '', old_title).strip()
    new_title = f'exp_{next_id}: {desc}'
    conn.execute('UPDATE tasks SET title = ? WHERE id = ?', (new_title, task_id))
    next_id += 1
conn.commit()
```

### For completed exp_AUTO tasks

Read run summaries and insert into SQLite:

```python
import json, subprocess, re, sqlite3
from datetime import datetime, timezone

# Get exp_AUTO done tasks
r = subprocess.run(['hermes', 'kanban', 'list', '--status', 'done', '--json'], 
                   capture_output=True, text=True, timeout=30)
tasks = json.loads(r.stdout)
auto_tasks = [t for t in tasks if 'exp_AUTO' in t.get('title', '')]

conn = sqlite3.connect('~/.hermes/prometheus.db')
next_id = max([int(re.search(r'(\d+)', x).group(1)) 
               for x in conn.execute('SELECT id FROM experiments').fetchall() 
               if re.search(r'(\d+', x[0])]) + 1

for task in auto_tasks:
    r2 = subprocess.run(['hermes', 'kanban', 'show', task['id'], '--json'],
                       capture_output=True, text=True, timeout=15)
    detail = json.loads(r2['output'])
    
    hypothesis = re.sub(r'^exp_AUTO:\s*', '', task['title']).strip()[:200]
    result = ""
    for run in detail.get('runs', []):
        if run.get('summary'):
            result = run['summary']
            break
    
    if result and len(result) > 20:
        exp_id = f'exp_{next_id}'
        next_id += 1
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO experiments
               (id, hypothesis, result, status, confidence_change, tags,
                domain, model, created_at, completed_at)
               VALUES (?, ?, ?, 'completed', 0, '[]', '', 'xiaomi/mimo-v2.5', ?, ?)""",
            (exp_id, hypothesis, result, now, now)
        )

conn.commit()
conn.close()
```

## Prevention

NEVER use `exp_AUTO` in experiment task titles. Always use `exp_NNN:` format
where NNN is the next sequential ID from SQLite.

The `batch_create_tasks.py` script now handles this automatically.
