#!/usr/bin/env python3
"""
rag_index_guard.py — keep the RAG experiment embedding index fresh.

ROOT-CAUSE FIX (2026-06-08): the experiment indexer was never scheduled — the
systemd hermes-rag.service only ran `serve` (which just sleeps), so the index
only advanced when a human ran `experiment_rag.py index` by hand. Combined with
an mtime short-circuit that silently skipped new files, the index drifted stale.

This guard runs on a timer. It compares the count of completed experiments in
prometheus.db against the count of experiments WITH a stored embedding in rag.db.
If drift exceeds DRIFT_THRESHOLD (or MAX_STALE_SECONDS elapsed) AND the GPU is not
busy, it indexes the missing experiment embeddings (reusing the live embedding
server). A flock prevents overlapping runs. Stays SILENT when nothing to do.
"""
import os, sys, time, fcntl, sqlite3, subprocess
from db_retry import get_db

HERMES = os.path.expanduser("~/.hermes")
PROM_DB = f"{HERMES}/prometheus.db"
RAG_DB = f"{HERMES}/rag/rag.db"
LOCK = f"{HERMES}/.rag_index_guard.lock"
LOG = f"{HERMES}/logs/rag_index_guard.log"
STAMP = f"{HERMES}/.rag_index_guard_last"

DRIFT_THRESHOLD = 10          # re-index when this many experiments lack embeddings
MAX_STALE_SECONDS = 5 * 60   # re-index when 5 minutes since last successful index
GPU_BUSY_UTIL = 90            # skip if GPU utilization % above this (embedding is lightweight HTTP calls)

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

def gpu_busy():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        utils = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        return any(u > GPU_BUSY_UTIL for u in utils)
    except Exception:
        return False  # if we can't tell, don't block

def counts():
    pc = get_db(readonly=True)
    total = pc.execute("SELECT COUNT(*) FROM experiments WHERE status='completed'").fetchone()[0]
    pc.close()
    rc = get_db(db_path=RAG_DB, readonly=True)
    indexed = rc.execute("SELECT COUNT(*) FROM experiments WHERE embedding_file IS NOT NULL").fetchone()[0]
    rc.close()
    return total, indexed

def last_index_age():
    try:
        return time.time() - os.path.getmtime(STAMP)
    except Exception:
        return 1e12

def main():
    lf = open(LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # another run in progress; stay silent

    try:
        total, indexed = counts()
        drift = total - indexed
        stale = last_index_age() > MAX_STALE_SECONDS
        if drift < DRIFT_THRESHOLD and not stale:
            return  # healthy; silent
        if gpu_busy():
            log(f"drift={drift} total={total} indexed={indexed} but GPU busy — deferring")
            return

        log(f"drift={drift} total={total} indexed={indexed} stale={stale} — indexing")
        # ROOT-CAUSE FIX (2026-06-08): purge stale mirror rows (experiments that no
        # longer exist in prometheus.db and have no embedding) so they don't accumulate
        # from INSERT OR REPLACE churn and inflate the missing-embedding count.
        try:
            pc = get_db(readonly=True)
            real_ids = set(x[0] for x in pc.execute("SELECT id FROM experiments WHERE status='completed'"))
            pc.close()
            rc = get_db(db_path=RAG_DB)
            # Also purge orphan rows with NULL id (SQLite can not match NULL via id=?)
            rc.execute("DELETE FROM experiments WHERE id IS NULL AND embedding_file IS NULL")
            stale_ids = [x[0] for x in rc.execute("SELECT id FROM experiments WHERE embedding_file IS NULL")
                         if x[0] not in real_ids]
            if stale_ids:
                rc.executemany("DELETE FROM experiments WHERE id=? AND embedding_file IS NULL",
                               [(s,) for s in stale_ids])
                rc.commit()
                log(f"purged {len(stale_ids)} stale mirror rows")
            rc.close()
        except Exception as e:
            log(f"stale-row purge skipped: {e}")

        sys.path.insert(0, f"{HERMES}/scripts")
        import experiment_rag as r
        url = r.start_embedding_server()  # reuses live server on :9150
        r._index_prometheus_experiments(url, only_missing=True)
        total2, indexed2 = counts()
        with open(STAMP, "w") as f:
            f.write(str(time.time()))
        # Also update the index_stats table so experiment_rag.py status shows accurate info
        ts = time.strftime('%Y-%m-%dT%H:%M:%S')
        rc2 = get_db(db_path=RAG_DB)
        rc2.execute("INSERT OR REPLACE INTO index_stats (key, value, updated_at) VALUES (?, ?, ?)",
                    ("last_index", ts, time.time()))
        rc2.execute("INSERT OR REPLACE INTO index_stats (key, value, updated_at) VALUES (?, ?, ?)",
                    ("experiment_count", str(indexed2), time.time()))
        rc2.commit()
        rc2.close()
        log(f"done — total={total2} indexed={indexed2} (was indexed={indexed})")
    except Exception as e:
        log(f"ERROR: {e}")
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()

if __name__ == "__main__":
    main()
