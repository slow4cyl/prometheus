#!/usr/bin/env python3
"""
Automated backup of prometheus.db — retains the most recent backups.

Run by cron every 15 minutes. Keeps MAX_BACKUPS most recent backups;
older ones are pruned automatically.

Uses the SQLite online-backup API (sqlite3.Connection.backup), NOT a file
copy: prometheus.db is a live WAL database written concurrently by crons
and workers, and a plain copy taken mid-write (without the -wal file) can
be torn/corrupt. The backup API takes a consistent snapshot without
blocking writers. The snapshot is written to a .partial file, verified
with PRAGMA quick_check, then atomically renamed into place — a crash
mid-backup can never leave a plausible-looking broken backup.

Backup location: ~/.hermes/backups/prometheus-db/
"""

import os
import sqlite3
import time
from pathlib import Path

BACKUP_DIR = Path.home() / ".hermes" / "backups" / "prometheus-db"
SOURCE = Path.home() / ".hermes" / "prometheus.db"
MAX_BACKUPS = 10  # 2.5h of rolling coverage at 15-min intervals (was 28/7h — the
                  # 1.8 GB snapshots dominated the 75 GB backups/ dir; manual
                  # pre-migration snapshots cover longer-horizon rollback separately)


def backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"prometheus-{timestamp}.db"
    partial = BACKUP_DIR / f"prometheus-{timestamp}.partial"

    try:
        # Consistent online snapshot of the live WAL database.
        src = sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True, timeout=60)
        dst = sqlite3.connect(partial)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # Verify the snapshot is a readable, sane database before promoting it.
        check_conn = sqlite3.connect(f"file:{partial}?mode=ro", uri=True)
        try:
            result = check_conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            check_conn.close()
        if result != "ok":
            raise RuntimeError(f"quick_check failed: {result}")

        # The snapshot is WAL-mode, so connections leave empty -wal/-shm
        # sidecars next to it; drop them before promoting the snapshot.
        for suffix in ("-wal", "-shm"):
            Path(f"{partial}{suffix}").unlink(missing_ok=True)
        os.replace(partial, dest)
    except Exception as e:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{partial}{suffix}").unlink(missing_ok=True)
        print(f"ERROR: backup failed: {e}")
        return False

    print(f"OK: {dest.name} ({dest.stat().st_size:,} bytes)")

    # Prune old backups (and any stale .partial* from interrupted runs).
    for stale in BACKUP_DIR.glob("prometheus-*.partial*"):
        if time.time() - stale.stat().st_mtime > 3600:
            stale.unlink(missing_ok=True)
            print(f"Pruned stale partial: {stale.name}")
    backups = sorted(BACKUP_DIR.glob("prometheus-*.db"), key=lambda p: p.stat().st_mtime)
    while len(backups) > MAX_BACKUPS:
        oldest = backups.pop(0)
        oldest.unlink()
        print(f"Pruned: {oldest.name}")

    return True


if __name__ == "__main__":
    backup()
