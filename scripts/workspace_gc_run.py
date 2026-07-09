import os
#!/usr/bin/env python3
"""Wrapper: runs workspace_gc.py with default TTLs (deletion is now the default)."""
import subprocess
import sys
result = subprocess.run(
    [sys.executable, os.path.expanduser("~/.hermes/scripts/workspace_gc.py")],
    capture_output=True, text=True
)
print(result.stdout)
if result.stderr:
    print(result.stderr, file=sys.stderr)
sys.exit(result.returncode)
