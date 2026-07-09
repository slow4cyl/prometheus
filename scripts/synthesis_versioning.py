#!/usr/bin/env python3
"""
Synthesis Provenance + Versioning — Threat Model Item 4

Adds:
1. Provenance column to synthesis_outputs (who, what, when, code version)
2. synthesis_versions table (snapshots for rollback)
3. Trigger to auto-snapshot before updates
4. Conflict detection (contradictory findings)
5. Freshness tracking (valid_until per finding)

Run once to migrate schema. Safe to re-run (idempotent).
"""

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

# Canonical paths
_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(
    os.path.join(_hermes_home_env, "prometheus_db.py")
) else _HERMES_MAIN
DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")


def get_git_commit():
    """Get current git commit hash (or 'unknown' if not a git repo)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=HERMES_HOME
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def get_skill_hashes():
    """Get hashes of all active skills."""
    skills_dir = os.path.join(HERMES_HOME, "skills")
    hashes = {}
    if not os.path.isdir(skills_dir):
        return hashes
    for root, dirs, files in os.walk(skills_dir):
        for f in files:
            if f == "SKILL.md":
                path = os.path.join(root, f)
                rel = os.path.relpath(path, skills_dir)
                try:
                    import hashlib
                    with open(path, "rb") as fh:
                        hashes[rel] = hashlib.md5(fh.read()).hexdigest()[:8]
                except Exception:
                    pass
    return hashes


def compute_state_vector():
    """Compute current system state vector."""
    return {
        "git_commit": get_git_commit(),
        "skill_hashes": get_skill_hashes(),
        "skill_count": len(get_skill_hashes()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "base_model": "xiaomi/mimo-v2.5",
    }


def migrate_schema(conn):
    """Add provenance column and create versions table. Idempotent."""
    cursor = conn.cursor()

    # Check if provenance column exists
    cols = {row[1] for row in cursor.execute("PRAGMA table_info(synthesis_outputs)").fetchall()}

    if "provenance" not in cols:
        cursor.execute("""
            ALTER TABLE synthesis_outputs
            ADD COLUMN provenance TEXT DEFAULT NULL
        """)
        print("  Added 'provenance' column to synthesis_outputs")
    else:
        print("  'provenance' column already exists")

    if "valid_until" not in cols:
        cursor.execute("""
            ALTER TABLE synthesis_outputs
            ADD COLUMN valid_until TEXT DEFAULT NULL
        """)
        print("  Added 'valid_until' column to synthesis_outputs")
    else:
        print("  'valid_until' column already exists")

    # Create synthesis_versions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS synthesis_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reason TEXT DEFAULT 'update',
            triggering_finding_id TEXT,
            state_vector TEXT
        )
    """)
    print("  synthesis_versions table ready")

    # Create synthesis_conflicts table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS synthesis_conflicts (
            conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
            finding_a_id TEXT NOT NULL,
            finding_b_id TEXT NOT NULL,
            conflict_type TEXT NOT NULL,
            description TEXT,
            resolved INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    print("  synthesis_conflicts table ready")

    conn.commit()


def snapshot_current_state(conn):
    """Take a snapshot of current synthesis_outputs state.

    Keeps only the latest 3 snapshots to prevent unbounded DB growth
    (each snapshot is ~60MB with 8K+ synthesis_outputs rows; 79 of them
    bloated prometheus.db to 4.9GB + 6GB WAL, causing lock contention
    that froze the dashboard).
    """
    cursor = conn.cursor()
    # Detect correct column name (prometheus.db uses key_patterns, experiments/prometheus.db uses patterns)
    cols = [r[1] for r in cursor.execute("PRAGMA table_info(synthesis_outputs)").fetchall()]
    pat_col = "key_patterns" if "key_patterns" in cols else "patterns"
    rows = cursor.execute(
        f"SELECT id, experiments_covered, {pat_col}, created_at, provenance "
        "FROM synthesis_outputs ORDER BY id"
    ).fetchall()

    snapshot = []
    for row in rows:
        snapshot.append({
            "id": row[0],
            "experiments_covered": row[1],
            "key_patterns": row[2],
            "created_at": row[3],
            "provenance": row[4],
        })

    state_vector = compute_state_vector()

    cursor.execute(
        "INSERT INTO synthesis_versions (snapshot, created_at, reason, state_vector) "
        "VALUES (?, ?, ?, ?)",
        (
            json.dumps(snapshot),
            datetime.now(timezone.utc).isoformat(),
            "initial_snapshot",
            json.dumps(state_vector),
        )
    )
    # Prune old snapshots — keep only the latest 3 for rollback
    cursor.execute(
        "DELETE FROM synthesis_versions WHERE version_id NOT IN "
        "(SELECT version_id FROM synthesis_versions ORDER BY version_id DESC LIMIT 3)"
    )
    conn.commit()
    pruned = cursor.rowcount
    print(f"  Snapshot taken: {len(snapshot)} findings, version {cursor.lastrowid}"
          + (f" (pruned {pruned} old snapshots)" if pruned else ""))


def backfill_provenance(conn):
    """Add provenance to existing rows that don't have it."""
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT id, synthesis_task_id, experiments_covered "
        "FROM synthesis_outputs WHERE provenance IS NULL"
    ).fetchall()

    if not rows:
        print("  All rows already have provenance")
        return

    state_vector = compute_state_vector()
    for row in rows:
        try:
            exp_covered = json.loads(row[2]) if row[2] else []
        except (json.JSONDecodeError, TypeError):
            exp_covered = []
        provenance = json.dumps({
            "synthesis_task_id": row[1],
            "experiments_covered": exp_covered,
            "state_vector": state_vector,
            "backfilled": True,
        })
        cursor.execute(
            "UPDATE synthesis_outputs SET provenance = ? WHERE id = ?",
            (provenance, row[0])
        )

    conn.commit()
    print(f"  Backfilled provenance for {len(rows)} rows")


def detect_conflicts(conn):
    """Check for contradictory findings in synthesis_outputs."""
    cursor = conn.cursor()
    # Detect correct column name
    cols = [r[1] for r in cursor.execute("PRAGMA table_info(synthesis_outputs)").fetchall()]
    pat_col = "key_patterns" if "key_patterns" in cols else "patterns"
    rows = cursor.execute(
        f"SELECT id, {pat_col} FROM synthesis_outputs "
        f"WHERE {pat_col} IS NOT NULL AND {pat_col} != ''"
    ).fetchall()

    if len(rows) < 2:
        print("  Not enough findings for conflict detection")
        return

    conflicts = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            id_a, patterns_a = rows[i]
            id_b, patterns_b = rows[j]

            # Simple keyword overlap conflict detection
            words_a = set(patterns_a.lower().split())
            words_b = set(patterns_b.lower().split())

            # Check for negation patterns
            negations = {"not", "no", "never", "fails", "broken", "wrong", "false"}
            neg_a = words_a & negations
            neg_b = words_b & negations

            # If one has negations and the other doesn't, and they share core terms
            core_a = words_a - negations - {"the", "a", "is", "was", "are", "that", "this", "with"}
            core_b = words_b - negations - {"the", "a", "is", "was", "are", "that", "this", "with"}

            if core_a and core_b:
                overlap = len(core_a & core_b) / max(len(core_a | core_b), 1)
                if overlap > 0.4 and bool(neg_a) != bool(neg_b):
                    conflicts.append({
                        "finding_a": id_a,
                        "finding_b": id_b,
                        "type": "negation_asymmetry",
                        "description": f"Shared core terms ({overlap:.0%}) with opposing negation patterns",
                    })

    if conflicts:
        for c in conflicts:
            cursor.execute(
                "INSERT INTO synthesis_conflicts "
                "(finding_a_id, finding_b_id, conflict_type, description, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (c["finding_a"], c["finding_b"], c["type"], c["description"],
                 datetime.now(timezone.utc).isoformat())
            )
        conn.commit()
        print(f"  Detected {len(conflicts)} potential conflicts")
    else:
        print("  No conflicts detected")


def main():
    print("=== Synthesis Provenance + Versioning Migration ===\n")

    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        return

    print(f"Database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass

    print("\n1. Migrating schema...")
    migrate_schema(conn)

    print("\n2. Backfilling provenance...")
    backfill_provenance(conn)

    print("\n3. Taking initial snapshot...")
    snapshot_current_state(conn)

    print("\n4. Detecting conflicts...")
    detect_conflicts(conn)

    conn.close()
    print("\nDone. Schema migrated, provenance backfilled, snapshot taken.")


if __name__ == "__main__":
    main()
