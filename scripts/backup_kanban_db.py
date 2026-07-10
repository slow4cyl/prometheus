#!/usr/bin/env python3
"""
Automated backup of kanban.db — retains last 28 backups (~14 hours at 30-min intervals).

Run by cron every 30 minutes. Keeps 28 most recent backups.
Older backups are pruned automatically.

Backup location: ~/.hermes/backups/kanban-db/

Uses exclusive file lock (F_LOCK) to prevent concurrent backups.
Verifies integrity on every backup before keeping it.

CONSISTENCY NOTE (hardened 2026-06-24):
The source kanban.db is a LIVE, WAL-mode database that 50+ workers and
~30 cron jobs write to continuously. A plain file copy (shutil.copy2)
snapshots the main .db file at an arbitrary instant and can capture it
mid-checkpoint / mid-page-allocation — producing a TORN copy that
references pages not yet flushed. That copy then fails integrity_check
with errors like "Tree 2 page N cell M: invalid page number K", even
though the original DB is perfectly healthy. This caused spurious
backup-failure alerts.

Fix: use SQLite's ONLINE BACKUP API (Connection.backup). It reads the
source through a real SQLite connection that is WAL-aware, so it produces
a transactionally CONSISTENT snapshot of the live DB. The resulting file
is a freshly-written, checkpointed (and compacted) database — so its size
will differ from the source, which is expected and fine.
"""

import fcntl
import os
import sqlite3
import sys
import time
from pathlib import Path
from prometheus_paths import BACKUPS_DIR, KANBAN_DB

BACKUP_DIR = Path(BACKUPS_DIR) / "kanban-db"
SOURCE = Path(KANBAN_DB)
LOCK_FILE = Path(BACKUPS_DIR) / "kanban-db.lock"
MAX_BACKUPS = 10  # 5h of rolling coverage at 30-min intervals (was 28/14h — trimmed with prometheus retention to reclaim backups/ space)


def _online_backup(src_path: Path, dest_path: Path) -> None:
    """Produce a consistent snapshot of a live WAL-mode SQLite DB.

    Opens the source read-only (cannot perturb live writers) and streams
    it page-by-page into a fresh destination DB via the online backup API.
    """
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        dest = sqlite3.connect(str(dest_path), timeout=30)
        try:
            # pages=-1 copies the whole DB in one step; SQLite handles
            # concurrent writers correctly under the backup API.
            src.backup(dest, pages=-1)
        finally:
            dest.close()
    finally:
        src.close()


def _cleanup_sidecars(db_path: Path) -> None:
    """Remove stray -wal/-shm sidecars for a (rejected) backup file."""
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass


def _prune_orphan_sidecars() -> None:
    """Sweep any -wal/-shm files in the backup dir.

    A finished backup file is a static snapshot and must never carry a live
    WAL/SHM. Sidecars here are always either (a) leftovers from old failed
    copy-based runs, or (b) empty artifacts created when something opened a
    backup read-only. Either way they are safe to remove."""
    for side in list(BACKUP_DIR.glob("kanban-*.db-wal")) + list(
        BACKUP_DIR.glob("kanban-*.db-shm")
    ):
        try:
            side.unlink()
        except OSError:
            pass


def backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # Acquire exclusive file lock — only one backup at a time
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("SKIP: Another backup is already running (F_LOCK held)")
        lock_fd.close()
        return False

    try:
        # Sweep orphaned sidecars left by previous failed/legacy runs
        _prune_orphan_sidecars()

        # Check source exists and is readable
        if not SOURCE.exists():
            print(f"ERROR: Source database not found: {SOURCE}")
            return False

        src_size = SOURCE.stat().st_size
        if src_size == 0:
            print(f"ERROR: Source database is empty: {SOURCE}")
            return False

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        dest = BACKUP_DIR / f"kanban-{timestamp}.db"

        # Consistent online backup of the live WAL-mode DB
        try:
            _online_backup(SOURCE, dest)
        except Exception as e:
            if dest.exists():
                os.remove(dest)
            _cleanup_sidecars(dest)
            print(f"ERROR: Online backup failed: {e}")
            return False

        dst_size = dest.stat().st_size if dest.exists() else 0
        if dst_size == 0:
            if dest.exists():
                os.remove(dest)
            _cleanup_sidecars(dest)
            print("ERROR: Backup produced an empty file")
            return False

        # Verify SQLite integrity of the snapshot
        try:
            db = sqlite3.connect(f"file:{dest}?mode=ro", uri=True, timeout=10)
            db.execute("PRAGMA busy_timeout=5000")
            result = db.execute("PRAGMA integrity_check").fetchone()
            db.close()
            if result[0] != "ok":
                os.remove(dest)
                _cleanup_sidecars(dest)
                print(f"ERROR: Integrity check failed: {result[0]}")
                return False
        except Exception as e:
            if dest.exists():
                os.remove(dest)
            _cleanup_sidecars(dest)
            print(f"ERROR: Integrity check error: {e}")
            return False

        # Backup connection leaves the file fully checkpointed; no sidecars
        # should remain, but clean up just in case.
        _cleanup_sidecars(dest)

        print(f"OK: {dest.name} ({dst_size:,} bytes, source {src_size:,} bytes)")

        # Prune old backups (only real .db files)
        backups = sorted(BACKUP_DIR.glob("kanban-*.db"), key=lambda p: p.stat().st_mtime)
        while len(backups) > MAX_BACKUPS:
            oldest = backups.pop(0)
            oldest.unlink()
            _cleanup_sidecars(oldest)
            print(f"Pruned: {oldest.name}")

        return True

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    success = backup()
    sys.exit(0 if success else 1)
