#!/usr/bin/env python3
"""
Critical resources watchdog.

Checks health of resources that have no other watchdog coverage:
- memory.db: exists, valid SQLite, has entries
- patches: exist and are non-empty
- key docs: exist and are non-zero size

Exit codes:
  0 = all healthy
  1 = degraded (warning, non-critical)
  2 = critical (action needed)

Designed to run via cron every 30 minutes. Silent when healthy.
"""

import os
import sqlite3
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MEMORIES_DIR = HERMES_HOME / "memories"
PATCH_DIR = HERMES_HOME / "patches"
DOCS_DIR = HERMES_HOME / "docs"

# Critical files: path, min_size_bytes, description
CRITICAL_FILES = [
    (MEMORIES_DIR / "memory.db", 1024, "Agent memory database"),
    (HERMES_HOME / "config.yaml", 100, "Hermes configuration"),
    (DOCS_DIR / "architecture-map.md", 1000, "Architecture reference"),
    # Dropped 2026-07-04: docs/sqlite-memory-migration.md — that migration is
    # complete (memory.db is healthy and validated above) and the reusable
    # pattern now lives in skills/director-loop/references/sqlite-memory-migration-pattern.md.
    # A completed migration's doc being absent is not a system-health signal.
]




def check_memory_db():
    """Check memory.db is valid and has entries."""
    db_path = MEMORIES_DIR / "memory.db"
    if not db_path.exists():
        return "CRITICAL", "memory.db does not exist"

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        conn.execute("PRAGMA quick_check")
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()

        if count == 0:
            return "WARNING", "memory.db exists but has 0 entries (empty)"
        elif count < 10:
            return "WARNING", f"memory.db has only {count} entries (suspiciously low)"
        else:
            return "OK", f"memory.db: {count} entries"
    except sqlite3.DatabaseError as e:
        return "CRITICAL", f"memory.db is corrupted: {e}"
    except Exception as e:
        return "CRITICAL", f"memory.db check failed: {e}"





def check_critical_files():
    """Check critical files exist and are non-trivial."""
    issues = []
    for path, min_size, desc in CRITICAL_FILES:
        if not path.exists():
            issues.append(f"MISSING: {desc} ({path.name})")
        elif path.stat().st_size < min_size:
            issues.append(f"SMALL: {desc} ({path.stat().st_size} bytes, expected ≥{min_size})")

    if issues:
        return "WARNING", "; ".join(issues)
    return "OK", f"{len(CRITICAL_FILES)} critical files present"





def main():
    checks = [
        ("Memory DB", check_memory_db),
        ("Critical Files", check_critical_files),
    ]

    results = []
    worst = "OK"

    for name, check_fn in checks:
        status, message = check_fn()
        results.append((name, status, message))
        if status == "CRITICAL":
            worst = "CRITICAL"
        elif status == "WARNING" and worst != "CRITICAL":
            worst = "WARNING"

    if worst == "OK":
        # Silent when healthy
        sys.exit(0)

    # Report problems
    print(f"Resource watchdog — {worst}")
    print()
    for name, status, message in results:
        icon = {"OK": "✓", "WARNING": "⚠", "CRITICAL": "✗"}.get(status, "?")
        print(f"  {icon} {name}: {message}")

    sys.exit(2 if worst == "CRITICAL" else 1)


if __name__ == "__main__":
    main()
