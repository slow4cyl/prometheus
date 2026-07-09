#!/usr/bin/env python3
"""
db_retry — SQLite connection helper with retry logic for WAL-mode contention.

Usage:
    from db_retry import get_db
    conn = get_db()  # returns a RetryConnection wrapping sqlite3.Connection
    
    # Use normally — execute/fetchone/fetchall have retry logic built in
    conn.execute("SELECT ...")
    row = conn.fetchone()
    conn.commit()

All cron scripts should use this instead of raw sqlite3.connect() to avoid
"database is locked" errors under concurrent access from 50 workers + 30 cron jobs.
"""
import sqlite3
import time
import os

PROMETHEUS_DB = os.path.expanduser("~/.hermes/prometheus.db")
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
RAG_DB = os.path.expanduser("~/.hermes/rag/rag.db")

# Contention tuning (2026-07-02). The old values (busy_timeout=800ms, ~1.5s of
# retry sleeps) assumed locks clear in microseconds — true for disk I/O, false
# for TRANSACTIONS: intake and the detector held the write lock for their whole
# run (up to minutes), so every other writer died with "database is locked"
# (~100 cron failures/day). Endurance must exceed the longest writer hold:
#   busy_timeout 30s   — SQLite queues the waiter in-process, cheap
#   6 retries, 0.25s base exponential — total endurance ≈ 6x30s + 8s ≈ 3 min
# The long holders were also fixed (per-result / per-pass commits), so in
# practice waits are sub-second; this is the belt to that suspenders.
MAX_RETRIES = 6
BASE_DELAY = 0.25
BUSY_TIMEOUT = 30000


class RetryConnection:
    """Wrapper around sqlite3.Connection that retries on 'database is locked'."""
    
    def __init__(self, conn):
        self._conn = conn
    
    def execute(self, sql, params=None):
        for attempt in range(MAX_RETRIES):
            try:
                if params is not None:
                    return self._conn.execute(sql, params)
                return self._conn.execute(sql)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                    time.sleep(BASE_DELAY * (2 ** attempt))
                    continue
                raise
    
    def executemany(self, sql, params):
        for attempt in range(MAX_RETRIES):
            try:
                return self._conn.executemany(sql, params)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                    time.sleep(BASE_DELAY * (2 ** attempt))
                    continue
                raise
    
    def executescript(self, sql):
        for attempt in range(MAX_RETRIES):
            try:
                return self._conn.executescript(sql)
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                    time.sleep(BASE_DELAY * (2 ** attempt))
                    continue
                raise
    
    def fetchone(self):
        return self._conn.fetchone()
    
    def fetchall(self):
        return self._conn.fetchall()
    
    def commit(self):
        for attempt in range(MAX_RETRIES):
            try:
                return self._conn.commit()
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                    time.sleep(BASE_DELAY * (2 ** attempt))
                    continue
                raise
    
    def close(self):
        self._conn.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self._conn.close()
    
    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_db(db_path=None, readonly=False):
    """Get a SQLite connection with WAL mode, busy timeout, and retry logic."""
    if db_path is None:
        db_path = PROMETHEUS_DB
    
    if readonly:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        conn = sqlite3.connect(db_path, timeout=30)
    
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    
    return RetryConnection(conn)
