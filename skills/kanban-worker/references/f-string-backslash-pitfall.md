# F-String Backslash Pitfall — Broadened Scope

**Added Cycle #223** — extends the original Cycle #191 heredoc-only documentation.

## The Problem

On Python ≤3.11, f-strings cannot contain backslash characters inside the expression part. This causes `SyntaxError: f-string expression part cannot include a backslash`.

This applies to **any** Python code containing f-strings with regex patterns:
- `python3 << 'EOF' ... EOF` heredocs (original documentation, Cycle #191)
- `write_file` scripts that pass `write_file`'s lint check but fail at runtime
- Any Python 3.11 or earlier execution context

## Examples That FAIL

```python
# In a heredoc:
python3 << 'EOF'
import re
ids = {f'exp_{m.group(1)}' for m in re.finditer(r'exp_(\d+)', text)}  # SyntaxError
EOF

# Via write_file (write_file's lint catches this before execution):
import re
print(f'IDs: {sorted(ids, key=lambda x: int(re.search(r"(\d+)", x).group(1)))}')  # Lint error
```

## Fixes

**Option A: Precompute outside f-string (preferred)**
```python
import re
matches = re.findall(r'exp_(\d+)', text)
ids = {f'exp_{m}' for m in matches}  # OK — no backslash in f-string expression
```

**Option B: Use .format() or %s**
```python
import re
for m in re.finditer(r'exp_(\d+)', text):
    print('exp_{}'.format(m.group(1)))  # OK
```

**Option C: Use string concatenation**
```python
import re
for m in re.finditer(r'exp_(\d+)', text):
    print('exp_' + m.group(1))  # OK
```

## Why write_file Catches It

`write_file` runs syntax checks on `.py` files. On Python 3.11 (current system), the f-string backslash restriction is enforced at parse time, so `write_file`'s lint catches it as a SyntaxError before the file is even written. This is helpful — it prevents silent runtime failures.

## Key Insight

The fix is NOT "write to file then execute" (that's for TIRITH pipe blocks). The fix is to **avoid f-strings with regex backslashes entirely**. Precompute the regex match, then use the clean result in the f-string.
