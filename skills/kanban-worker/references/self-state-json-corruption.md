# self_state.json Structural Corruption — Diagnosis and Fix

Added: Cycle #217 (2026-06-01)

## Problem

Synthesis workers writing to `self_state.json` can introduce structural corruption that prevents `json.loads()` from parsing the file. This blocks the synthesis worker, Director, and any tool that reads self_state.json.

## Observed Corruption Patterns

### Pattern 1: Missing keys in nested objects

When a synthesis worker appends experiment entries to `experiment_notes`, it may write `{...}` instead of `"exp_NNN": {...}` — the key name is omitted.

**Symptom**: `json.loads()` fails with:
```
json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line NNN column 5
```

**Example** (CORRUPT):
```json
"experiment_notes": {
    "exp_581": { "title": "...", ... },
    {                          ← MISSING KEY
      "title": "Token-probability divergence detection",
      ...
    }
}
```

### Pattern 2: Bracket type mismatch

A synthesis worker may write `],` (closing bracket for an array) where `}` (closing bracket for an object) is needed.

**Symptom**: Bracket imbalance: `[ balance: -1, { balance: 1`

## Diagnosis Technique

### Step 1: Check total bracket balance

```python
with open(os.path.expanduser('~/.hermes/self_state.json'), 'r') as f:
    content = f.read()

total = {'{': 0, '}': 0, '[': 0, ']': 0}
for c in content:
    if c in total: total[c] += 1

print("{ balance: %d, [ balance: %d" % (
    total['{'] - total['}'],
    total['['] - total[']']
))
```

### Step 2: Find the corruption location

For bracket imbalance (extra `]`):
```python
bal = 0
for i, c in enumerate(content):
    if c == '[': bal += 1
    elif c == ']':
        bal -= 1
        if bal < 0:
            line = content[:i].count('\n') + 1
            print("Extra ] at pos %d (line %d)" % (i, line))
            print("Context:", repr(content[max(0,i-80):i+20]))
            break
```

### Step 3: Identify missing experiment IDs

For missing-key corruption, scan for standalone `{` lines without a preceding key:
```python
import re
lines = content.split('\n')
for i, line in enumerate(lines):
    if line.strip() == '{':
        for j in range(i+1, min(i+10, len(lines))):
            m = re.search(r'"title"\s*:\s*"([^"]+)"', lines[j])
            if m:
                print("Line %d: missing key for '%s'" % (i+1, m.group(1)[:60]))
                break
```

### Pattern 3: Orphaned strings after array close (June 2026)

The `synthesis_merger.py` cron job appends to arrays in `self_state.json` and can produce orphaned strings AFTER the JSON object's closing brace `}`.

**Symptom**: `json.decoder.JSONDecodeError: Extra data: line NNN column 2`

**Example** (CORRUPT):
```json
  "synthesized_experiments": [
    "exp_1109",
    "exp_1110"
  ]
}0.8732 vs TF-IDF alone 0.9572.",
    "exp_2833: exp_2833 REFUTED: ..."
  ],
  "done": [
    "exp_3610"
  ]
}
```

The valid JSON ends at the first `}`, and everything after is orphaned synthesis output text.

**Fix**: Use `json.JSONDecoder().raw_decode()` to find valid JSON boundary, then truncate:

```python
import json
with open('self_state.json') as f:
    content = f.read()

decoder = json.JSONDecoder()
obj, end = decoder.raw_decode(content)

# Write back only the valid JSON
with open('self_state.json', 'w') as f:
    json.dump(obj, f, indent=2, ensure_ascii=False)
```

**Detection**: `json.loads()` fails with "Extra data" at a specific position. The orphaned data is usually redundant with what's already in the valid JSON.

## Director Pass Pre-Check (Step 0)

**Every Director pass MUST verify self_state.json integrity before proceeding.** If the file is corrupt, `director.py` crashes and later steps (unsynthesis detection, queue scoring) fail silently or with confusing errors.

```python
import json, os
path = os.path.expanduser('~/.hermes/self_state.json')
try:
    with open(path) as f:
        json.load(f)
    print("self_state.json: VALID")
except json.JSONDecodeError as e:
    print(f"self_state.json: CORRUPT — {e}")
    # Fix: truncate at valid JSON boundary
    with open(path) as f:
        data = f.read()
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(data)
    with open(path, 'w') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    print(f"Fixed: truncated {len(data)} → {len(json.dumps(obj))} chars")
```

Run this as the VERY FIRST step of every Director pass, before board state dump, queue scoring, or task creation. If the fix removes >50KB of data, log a warning — the merger may be writing corrupted output repeatedly.

## Fix Patterns

### Fix 1: Bracket type mismatch
Use `patch()` for targeted fixes (e.g., `],` → `}`).

### Fix 2: Missing keys
Use Python file I/O to add missing keys by matching titles to experiment IDs.

### Step 4: Validate
Always validate with `json.loads()` after fixing.

## Prevention

Synthesis workers should validate JSON before writing:
```python
json.loads(json.dumps(data))  # Validate before write
```
