# TIRITH Variation Selector Block — Extended Scope (2026-06-02)

## Original Observation (2026-05-31)
TIRITH blocks emoji/Unicode variation selectors in `python3 << 'PYEOF' ... PYEOF` heredocs.

## Extended Observation (2026-06-02)
The same block also applies to `python3 -c "..."` inline commands. During Director pass 245, a `python3 -c "..."` command containing `⚠️` in a status string was blocked by `tirith:variation_selector`. The command was:

```python
python3.11 -c "
import json, subprocess, os, datetime
...
status = '⚠️ POTENTIALLY STUCK'  # <-- This emoji triggered the block
..."
```

## Affected Patterns
1. `python3 << 'PYEOF' ... PYEOF` (heredocs) — original
2. `python3 -c "..."` (inline scripts) — extended
3. `python3.11 -c "..."` (versioned inline scripts) — extended

## Workaround
Write the script to a file first, then execute:
```bash
# Instead of python3 -c "..." with emoji:
write_file /tmp/script.py "..."  # no emoji in content
python3.11 /tmp/script.py
```

## Root Cause
TIRITH scans command arguments for Unicode variation selectors (VS1-256). Both heredocs and `-c` inline scripts pass through the same scanner. Any emoji containing variation selectors (⚠️, ✅, ❌, etc.) triggers the block.
