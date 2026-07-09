# Task ID Format Collision Pitfall (June 3 2026)

## Problem

When manually creating tasks (NOT via `batch_create_tasks.py`), using sequential hex IDs like `t_{next_id:08x}` produces collisions with existing task IDs.

**Reproduction:**
```python
next_id = 4147  # from kanban.db max exp_NNN
task_id = f't_{next_id:08x}'  # → "t_00004147"
# But t_00004147 already exists from a previous pass!
# INSERT fails with: sqlite3.IntegrityError: UNIQUE constraint failed: tasks.id
```

**Why it happens:** The hex encoding of sequential numbers (4147 → `1033`) produces short hex strings. Over many passes, these accumulate and collide with IDs from earlier sessions. The format `t_00004147` looks like a one-time ID but is actually a collision.

**How `batch_create_tasks.py` avoids it:** Uses hash-based IDs: `t_` + MD5 hash of `(task_title + timestamp)` → `t_d646cf38`. These are practically collision-free.

## Fix

Use hash-based IDs for manual task creation:

```python
import hashlib, time

task_id = 't_' + hashlib.md5(f'{title}_{time.time()}'.encode()).hexdigest()[:8]
```

Or use UUID-based:
```python
import uuid
task_id = 't_' + uuid.uuid4().hex[:8]
```

**Never use** `f't_{next_id:08x}'` for manual task creation — the sequential experiment counter is for `exp_NNN` titles, not for `t_XXXX` task IDs.

## Detection

When `INSERT INTO tasks` raises `UNIQUE constraint failed: tasks.id` after generating a sequential hex ID, this is the collision. Retry with a hash-based ID.

## Key Distinction

- `exp_NNN` (experiment ID in title): sequential, from `kanban.db` max+1 — use this for experiment numbering
- `t_XXXX` (task ID primary key): must be unique — use hash-based generation

## Related Pitfalls

- `director-experiment-id-conflict-pitfall.md` — exp_NNN conflicts between running/done tasks
- `experiment-id-reuse-across-passes.md` — experiment number reuse across Director passes