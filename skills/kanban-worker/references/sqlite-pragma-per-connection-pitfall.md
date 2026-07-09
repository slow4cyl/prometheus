# SQLite PRAGMA Per-Connection Pitfall

**Added:** 2026-06-02
**Context:** Diagnosing whether Optane-tuned PRAGMAs are applying in hermes_state.py

## The Problem

When checking SQLite PRAGMA settings with a diagnostic script, the values
showed defaults (synchronous=2/FULL, cache_size=-2000, mmap_size=0)
despite the source code clearly applying synchronous=OFF, cache_size=-1GB,
mmap_size=2GB, temp_store=MEMORY.

## Root Cause

SQLite PRAGMAs are **per-connection settings**, not database-wide. When you
open a fresh `sqlite3.connect()` in a diagnostic script, that connection
sees the database header defaults — NOT the per-connection overrides that
the application sets on its own connection.

```
Application connection:  synchronous=OFF  (set by hermes_state.py __init__)
Diagnostic connection:   synchronous=2    (database header default)
```

Both connections point to the same file. They see different PRAGMA values
because PRAGMAs live in the connection state, not the file.

## How to Verify Correctly

**Wrong approach** (opens fresh connection):
```python
conn = sqlite3.connect('state.db')
print(conn.execute('PRAGMA synchronous').fetchone()[0])  # Shows default!
```

**Correct approach** (check source code):
```bash
grep -n "PRAGMA synchronous" ~/.hermes/hermes-agent/hermes_state.py
# If the line exists and is unconditional (no if/try guard), it applies.
```

**Or** (set PRAGMAs on the same connection, then query):
```python
conn = sqlite3.connect('state.db')
conn.execute('PRAGMA synchronous=OFF')
val = conn.execute('PRAGMA synchronous').fetchone()[0]
print(val)  # Shows 0 (OFF) — correct
```

## When This Matters

- Debugging Optane-tuned databases (synchronous=OFF, mmap, etc.)
- Verifying WAL mode applied correctly
- Any time you need to confirm PRAGMA settings are active

## The Actual Performance Proof

If FTS queries return sub-millisecond results (0.67ms on 64K messages),
the PRAGMAs are working. Default SQLite on consumer SSDs would be 3-10x
slower. Performance is the ground truth, not PRAGMA queries on fresh
connections.
