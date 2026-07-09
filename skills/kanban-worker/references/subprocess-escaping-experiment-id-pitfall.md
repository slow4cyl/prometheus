# Python Subprocess Double-Escaping Pitfall (June 3 2026)

## The Problem

When fetching the next experiment ID via `subprocess.run(['python3', '-c', '...'])`, regex patterns with backslashes inside the inner Python code get double-escaped. The subprocess consumes one layer of backslashes, and the inner Python's `re` module consumes another — producing wrong results.

**Failing pattern:**
```python
result = subprocess.run(
    ['python3', '-c', """
import sqlite3, re
c = sqlite3.connect('~/.hermes/kanban.db')
r = c.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
ids = [int(re.search(r'exp_(\\d+)', x[0]).group(1)) for x in r if re.search(r'exp_(\\d+)', x[0])]
print(max(ids)+1 if ids else 1)
"""],
    capture_output=True, text=True
)
next_id = int(result.stdout.strip())  # Returns 1 instead of 3223!
```

**What happens:** The `\\d+` in the heredoc becomes `\d+` after one layer of escaping, but subprocess passes it to the inner Python where `r'exp_(\d+)'` is correct — EXCEPT the heredoc already consumed one backslash, so the inner Python sees `r'exp_(d+)'` (no backslash), which matches literal `d+` instead of `\d+`. With no matches, the query returns `max()+1 = 1`.

## Detection

- Script creates tasks starting from `exp_1` instead of the expected `exp_NNNN`
- `print(max(ids)+1 if ids else 1)` prints `1` when kanban.db has thousands of experiments

## Fix Options (in preference order)

**Option 1: Compute in outer Python, pass as argument (preferred)**
```python
# Compute ID in the calling context, not inside subprocess
import sqlite3, re
c = sqlite3.connect('~/.hermes/kanban.db')
r = c.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
ids = [int(re.search(r'exp_(\d+)', x[0]).group(1)) for x in r if re.search(r'exp_(\d+)', x[0])]
next_id = max(ids) + 1 if ids else 1
c.close()
# Then use next_id directly — no subprocess needed
```

**Option 2: Write script to temp file, execute file**
```python
import tempfile, subprocess
script = '''
import sqlite3, re
c = sqlite3.connect('~/.hermes/kanban.db')
r = c.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
ids = [int(re.search(r'exp_(\\d+)', x[0]).group(1)) for x in r if re.search(r'exp_(\\d+)', x[0])]
print(max(ids)+1 if ids else 1)
'''
with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
    f.write(script)
    tmp = f.name
result = subprocess.run(['python3', tmp], capture_output=True, text=True)
next_id = int(result.stdout.strip())
```

**Option 3: Hardcode start ID when creating a batch**
```python
START_ID = 3223  # Get this once at the top of the script
for i, item in enumerate(selected):
    exp_id = START_ID + i
    # ...
```

## Why This Happens

Python's `subprocess.run` with a list argument passes each element as a separate argv entry — no shell expansion. But the string containing the inner Python code still goes through Python's own string escaping before reaching subprocess. In a heredoc (`"""..."""`), backslashes are interpreted by Python's parser first. `\\d` becomes `\d` in the string, which subprocess passes to the inner Python. But if there are TWO levels (heredoc + regex raw string conflict), you can lose backslashes.

The safest approach is Option 1: compute what you can in the calling context and avoid nested Python execution entirely.

## Related Pitfalls

- **read_file line number corruption**: Same category (tool output format leaking into file content). See `references/read-file-line-number-corruption.md`.
- **TIRITH blocks `cat | python3` pipe**: Different mechanism, same workaround pattern (use heredoc or temp file). See main SKILL.md pitfalls section.
