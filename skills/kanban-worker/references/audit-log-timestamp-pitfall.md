# Audit Log Timestamp Python Formatting Pitfall (added 2026-06-02)

## Problem

When writing Director pass entries to `self_audit.log` using Python string formatting, using `%%` in a format string causes the timestamp to NOT be interpolated.

## Example That Fails

```python
import datetime
entry = '''
=== DIRECTOR PASS ===
Timestamp: %s
Cycle: 244
''' % datetime.datetime.now(datetime.timezone.utc).strftime('%%Y-%%m-%%d %%H:%%M:%%S UTC')
```

**Result:** `Timestamp: %Y-%m-%d %H:%M:%S UTC` — literal percent signs, not the date.

## Why It Happens

The `%%` is an escape sequence in Python's `%` string formatting operator. It produces a literal `%` character instead of interpolating. This is correct behavior for displaying a literal `%` (e.g., `"100%%"` → `"100%"`), but when used inside `strftime()`, it breaks the date formatting.

The code runs without error — the timestamp just silently becomes literal text. This is easy to miss because:
1. No exception is raised
2. The rest of the audit entry is correct
3. The literal `%Y-%m-%d` looks almost like a date at a glance

## Fix

Use single `%` for strftime, not doubled:

```python
# CORRECT — single % for strftime
ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
entry = 'Timestamp: %s' % ts

# ALSO CORRECT — f-string (avoids % formatting entirely)
ts = f'{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M:%S UTC}'
entry = f'Timestamp: {ts}'

# ALSO CORRECT — .format() method
ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
entry = 'Timestamp: {}'.format(ts)
```

## Prevention

When writing audit log entries in Director passes, use f-strings or separate the timestamp formatting into its own variable. This avoids the `%%` trap entirely.

## Context

This happened in the 2026-06-02 Director pass when writing the audit trail entry. The `%%Y-%%m-%%d` pattern was used inside a `%` format string, causing the timestamp to render as literal text.
