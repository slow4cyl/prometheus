"""
Prometheus State Database v2 — SQLite-backed state management.
Fixed for 20+ concurrent workers: context manager pattern, busy timeout, WAL mode.
"""
import sqlite3
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

# Canonical main hermes dir — resolved EXPLICITLY, never from the script location.
# This module has lived at both ~/.hermes/prometheus_db.py (worker cwd-copy, where
# dirname(__file__) happened to be right) and ~/.hermes/scripts/prometheus_db.py
# (canonical, where dirname(__file__) pointed at a GHOST scripts/prometheus.db with
# a pre-migration schema — 2026-07-08: intake + task-janitor crash-looped on it,
# "no such column: kanban_task_id" / "no such table: heartbeats"). expanduser makes
# the resolution location-proof no matter where a copy of this file runs from.
_HERMES_MAIN = os.path.expanduser("~/.hermes")

DB_PATH = os.path.join(_HERMES_MAIN, "prometheus.db")
DB_LOCK = DB_PATH + ".lock"

@contextmanager
def get_db():
    """Context manager — connection auto-closes on exit. NEVER leak connections.
    
    Uses fcntl.flock LOCK_SH (shared) for WAL mode. Multiple readers can
    access simultaneously. SQLite's internal WAL lock handles write
    serialization — the file lock only prevents concurrent file-level
    corruption, not DB-level contention.
    """
    import fcntl
    lock_fd = open(DB_LOCK, "a+")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)  # Shared lock — allows concurrent readers
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        # 30s wait on lock before failing. 5s was not enough: writer transactions
        # (intake batch, detector passes) can legitimately hold the write lock for
        # seconds; a waiter that gives up sooner than the longest holder dies with
        # "database is locked" (2026-07-02: ~100 cron failures/day from this).
        conn.execute("PRAGMA busy_timeout=30000")
        # ── Optane P5800x tuning ──
        # synchronous=NORMAL is the correct WAL setting: crash-safe against
        # process kills / OOM (synchronous=OFF was NOT — it corrupted the DB on
        # 2026-06-15 when a writer was killed mid-checkpoint; PLP only protects
        # against power loss, not SIGKILL/OOM). Costs ~2-5% throughput.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-1048576")    # 1GB page cache
        conn.execute("PRAGMA mmap_size=2147483648")   # 2GB mmap
        conn.execute("PRAGMA temp_store=MEMORY")      # Temp tables in RAM
        conn.execute("PRAGMA foreign_keys=ON")
        # Auto-checkpoint the WAL every 1000 pages (~4MB). Without this, the
        # WAL grew to 6GB under heavy writer load, causing lock contention
        # that froze the dashboard and caused worker write failures.
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

def query_db(sql, params=()):
    """Execute query, return list of dicts. Connection auto-closed."""
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def query_db_one(sql, params=()):
    """Execute query, return single row or None. Connection auto-closed."""
    results = query_db(sql, params)
    return results[0] if results else None

def execute_db(sql, params=()):
    """Execute statement. Connection auto-closed."""
    with get_db() as conn:
        conn.execute(sql, params)

def init_db():
    """Initialize database schema."""
    with get_db() as conn:
        conn.executescript("""
    CREATE TABLE IF NOT EXISTS system (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at REAL NOT NULL,
        completed_at REAL,
        status TEXT DEFAULT 'running',
        phase TEXT,
        summary TEXT,
        experiments_discovered INTEGER DEFAULT 0,
        experiments_synthesized INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS experiments (
        id TEXT PRIMARY KEY,
        cycle_id INTEGER REFERENCES cycles(id),
        hypothesis TEXT,
        result TEXT,
        status TEXT DEFAULT 'pending',
        confidence_change REAL DEFAULT 0,
        tags TEXT,
        domain TEXT,
        model TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        completed_at REAL,
        workspace_path TEXT,
        kanban_task_id TEXT
    );
    CREATE TABLE IF NOT EXISTS domains (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        confidence REAL DEFAULT 0.5,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS subtopics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_id INTEGER REFERENCES domains(id),
        topic TEXT NOT NULL,
        source_experiment TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS gaps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain_id INTEGER REFERENCES domains(id),
        description TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        closed_by_experiment TEXT,
        created_at REAL NOT NULL,
        closed_at REAL
    );
    CREATE TABLE IF NOT EXISTS curiosities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT NOT NULL,
        priority INTEGER DEFAULT 5,
        status TEXT DEFAULT 'active',
        source_experiment TEXT,
        resolved_by_experiment TEXT,
        created_at REAL NOT NULL,
        resolved_at REAL
    );
    CREATE TABLE IF NOT EXISTS skills (
        name TEXT PRIMARY KEY,
        category TEXT,
        created_at REAL NOT NULL,
        patched_at REAL,
        version TEXT
    );
    CREATE TABLE IF NOT EXISTS self_mods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id INTEGER REFERENCES cycles(id),
        type TEXT,
        description TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        cycle_id INTEGER REFERENCES cycles(id),
        entry_type TEXT,
        content TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS metrics_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        cycles_completed INTEGER,
        experiments_run INTEGER,
        experiments_completed INTEGER,
        knowledge_gaps_closed INTEGER,
        skills_created INTEGER,
        self_modifications INTEGER,
        cost_total REAL,
        cache_hit_rate REAL
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT,
        body TEXT,
        status TEXT,
        assignee TEXT,
        priority INTEGER DEFAULT 0,
        created_at REAL,
        started_at REAL,
        completed_at REAL,
        result TEXT,
        workspace_path TEXT
    );
    CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT REFERENCES tasks(id),
        kind TEXT,
        payload TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS capabilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL,
        source_experiment TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS constraints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS goals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        description TEXT NOT NULL,
        status TEXT DEFAULT 'active',
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS from_isaac (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_experiments_status ON experiments(status);
    CREATE INDEX IF NOT EXISTS idx_experiments_domain ON experiments(domain);
    CREATE INDEX IF NOT EXISTS idx_experiments_cycle ON experiments(cycle_id);
    CREATE INDEX IF NOT EXISTS idx_cycles_status ON cycles(status);
    CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
    CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(entry_type);
    """)
    print(f"Database initialized at {DB_PATH}")

# ── Helper functions (all use context manager — connections auto-close) ──

def set_system(key, value):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system (key, value, updated_at) VALUES (?, ?, ?)",
                     (key, json.dumps(value), time.time()))

def get_system(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM system WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

def add_cycle(phase=None, summary=None):
    with get_db() as conn:
        cur = conn.execute("INSERT INTO cycles (started_at, phase, summary) VALUES (?, ?, ?)",
                          (time.time(), phase, summary))
        return cur.lastrowid

def complete_cycle(cycle_id, summary=None):
    with get_db() as conn:
        conn.execute("UPDATE cycles SET completed_at = ?, status = 'completed', summary = COALESCE(?, summary) WHERE id = ?",
                     (time.time(), summary, cycle_id))

def add_experiment(exp_id, hypothesis=None, domain=None, cycle_id=None, kanban_task_id=None):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO experiments (id, hypothesis, domain, cycle_id, kanban_task_id, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (exp_id, hypothesis, domain, cycle_id, kanban_task_id, time.time()))

def complete_experiment(exp_id, result, tags=None, confidence_change=0, domain=None):
    try:
        confidence_change = float(confidence_change)
    except (TypeError, ValueError):
        confidence_change = 0.0
    with get_db() as conn:
        conn.execute(
            """UPDATE experiments SET result = ?, tags = ?, confidence_change = ?,
               status = 'completed', completed_at = ?, domain = COALESCE(?, domain) WHERE id = ?""",
            (result, json.dumps(tags or []), confidence_change, time.time(), domain, exp_id))

def get_unsynthesized_experiments():
    return query_db(
        "SELECT id, hypothesis, result, tags, domain FROM experiments WHERE status = 'completed' AND id NOT IN (SELECT DISTINCT source_experiment FROM subtopics WHERE source_experiment IS NOT NULL)")

def get_experiments_by_domain(domain):
    return query_db(
        "SELECT id, hypothesis, result, tags, confidence_change FROM experiments WHERE domain = ? ORDER BY completed_at",
        (domain,))

def add_domain(name, confidence=0.5):
    now = time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO domains (name, confidence, created_at, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET confidence = ?, updated_at = ?",
            (name, confidence, now, now, confidence, now))

def add_subtopic(domain_name, topic, source_experiment=None):
    with get_db() as conn:
        domain = conn.execute("SELECT id FROM domains WHERE name = ?", (domain_name,)).fetchone()
        if domain:
            conn.execute(
                "INSERT INTO subtopics (domain_id, topic, source_experiment, created_at) VALUES (?, ?, ?, ?)",
                (domain["id"], topic, source_experiment, time.time()))

def add_curiosity(text, priority=5, source_experiment=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO curiosities (text, priority, source_experiment, created_at) VALUES (?, ?, ?, ?)",
            (text, priority, source_experiment, time.time()))

def resolve_curiosity(curiosity_id, resolved_by_experiment):
    with get_db() as conn:
        conn.execute(
            "UPDATE curiosities SET status = 'resolved', resolved_by_experiment = ?, resolved_at = ? WHERE id = ?",
            (resolved_by_experiment, time.time(), curiosity_id))

def log_audit(entry_type, content, cycle_id=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_log (timestamp, cycle_id, entry_type, content) VALUES (?, ?, ?, ?)",
            (time.time(), cycle_id, entry_type, content))


def log_self_mod(mod_type, description, cycle_id=None):
    """Record a self-modification event in the self_mods table.

    ROOT-CAUSE FIX (2026-06-08): self_mods was an orphaned table — no code ever
    wrote to it, so the system had no per-event record of its own modifications
    (only a scalar counter in self_state.json). Call this at every self-mod site:
    skill create/patch (type='skill'), config change (type='config'), memory write
    (type='memory'), prompt edit (type='prompt').
    """
    valid = {"skill", "config", "memory", "prompt"}
    if mod_type not in valid:
        mod_type = "other"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO self_mods (cycle_id, type, description, created_at) VALUES (?, ?, ?, ?)",
            (cycle_id, mod_type, description, time.time()))

def get_metrics_summary():
    with get_db() as conn:
        return {
            "cycles": conn.execute("SELECT COUNT(*) as n FROM cycles").fetchone()["n"],
            "experiments_total": conn.execute("SELECT COUNT(*) as n FROM experiments").fetchone()["n"],
            "experiments_completed": conn.execute("SELECT COUNT(*) as n FROM experiments WHERE status = 'completed'").fetchone()["n"],
            "domains": conn.execute("SELECT COUNT(*) as n FROM domains").fetchone()["n"],
            "subtopics": conn.execute("SELECT COUNT(*) as n FROM subtopics").fetchone()["n"],
            "gaps_open": conn.execute("SELECT COUNT(*) as n FROM gaps WHERE status = 'open'").fetchone()["n"],
            "curiosities_active": conn.execute("SELECT COUNT(*) as n FROM curiosities WHERE status = 'active'").fetchone()["n"],
            "skills": conn.execute("SELECT COUNT(*) as n FROM skills").fetchone()["n"],
            "unsynthesized": conn.execute(
                "SELECT COUNT(*) as n FROM experiments WHERE status = 'completed' AND id NOT IN (SELECT DISTINCT source_experiment FROM subtopics WHERE source_experiment IS NOT NULL)"
            ).fetchone()["n"],
        }

def import_from_json(json_path=None):
    """Import existing self_state.json into SQLite."""
    if json_path is None:
        json_path = os.path.expanduser("~/.hermes/self_state.json")
    with open(json_path) as f:
        state = json.load(f)
    now = time.time()
    with get_db() as conn:
        for key in ["version", "agent_id", "last_updated"]:
            if key in state:
                conn.execute("INSERT OR REPLACE INTO system (key, value, updated_at) VALUES (?, ?, ?)",
                            (key, json.dumps(state[key]), now))
        identity = state.get("identity", {})
        for key, val in identity.items():
            if isinstance(val, str):
                conn.execute("INSERT OR REPLACE INTO system (key, value, updated_at) VALUES (?, ?, ?)",
                            (f"identity.{key}", json.dumps(val), now))
            elif isinstance(val, list):
                for item in val:
                    if key == "capabilities":
                        conn.execute("INSERT INTO capabilities (description, created_at) VALUES (?, ?)", (item, now))
                    elif key == "constraints":
                        conn.execute("INSERT INTO constraints (description, created_at) VALUES (?, ?)", (item, now))
        for exp in state.get("experiments", {}).get("completed", []):
            if isinstance(exp, dict):
                conn.execute(
                    "INSERT OR REPLACE INTO experiments (id, hypothesis, result, status, confidence_change, tags, created_at, completed_at) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)",
                    (exp.get("id", "?"), exp.get("hypothesis", ""), exp.get("result", ""),
                     exp.get("confidence_change", 0), json.dumps(exp.get("tags", [])), now, now))
            elif isinstance(exp, str):
                conn.execute("INSERT OR REPLACE INTO experiments (id, status, created_at) VALUES (?, 'completed', ?)", (exp, now))
        for d in state.get("knowledge_graph", {}).get("domains", []):
            conn.execute("INSERT OR REPLACE INTO domains (name, confidence, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (d["name"], d.get("confidence", 0.5), now, now))
            domain_row = conn.execute("SELECT id FROM domains WHERE name = ?", (d["name"],)).fetchone()
            if domain_row:
                for sub in d.get("subtopics", []):
                    conn.execute("INSERT INTO subtopics (domain_id, topic, created_at) VALUES (?, ?, ?)",
                                (domain_row["id"], sub, now))
                for gap in d.get("gaps", []):
                    conn.execute("INSERT INTO gaps (domain_id, description, status, created_at) VALUES (?, ?, 'open', ?)",
                                (domain_row["id"], gap, now))
        for q in state.get("curiosity_queue", []):
            text = q.get("text", str(q)) if isinstance(q, dict) else str(q)
            priority = q.get("priority", 5) if isinstance(q, dict) else 5
            conn.execute("INSERT INTO curiosities (text, priority, status, created_at) VALUES (?, ?, 'active', ?)",
                        (text, priority, now))
        skills_base = os.path.expanduser("~/.hermes/skills")
        for cat in ["research", "mlops", "software-development", "inference",
                     "hermes-textgrad", "hermes-best-models", "graduated-scope-system",
                     "autonomous-ai-agents", "devops", "system-administration"]:
            cat_path = os.path.join(skills_base, cat)
            if os.path.isdir(cat_path):
                for entry in os.listdir(cat_path):
                    if os.path.isfile(os.path.join(cat_path, entry, "SKILL.md")):
                        conn.execute("INSERT OR REPLACE INTO skills (name, category, created_at) VALUES (?, ?, ?)",
                                    (entry, cat, now))
                        # Record skill registration as a self-modification event.
                        conn.execute(
                            "INSERT INTO self_mods (cycle_id, type, description, created_at) VALUES (?, ?, ?, ?)",
                            (None, "skill", f"Registered skill: {entry} ({cat})", now))
        m = state.get("metrics", {})
        conn.execute(
            "INSERT INTO metrics_history (timestamp, cycles_completed, experiments_run, experiments_completed, knowledge_gaps_closed, skills_created, self_modifications) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now, m.get("autonomous_cycles_completed", 0), m.get("experiments_run", 0),
             m.get("experiments_completed", 0), m.get("knowledge_gaps_closed", 0),
             conn.execute("SELECT COUNT(*) as n FROM skills").fetchone()["n"],
             m.get("self_modifications", 0)))
    print(f"Imported from {json_path}")

if __name__ == "__main__":
    init_db()
    import_from_json()
    print("\nMetrics summary:")
    for k, v in get_metrics_summary().items():
        print(f"  {k}: {v}")

# ── Heartbeat functions ──

def write_heartbeat(task_id, worker_id=None, status="alive", details=None):
    """Write a heartbeat for a worker. Call every 2 minutes from worker scripts."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO heartbeats (task_id, worker_id, timestamp, status, details) VALUES (?, ?, ?, ?, ?)",
            (task_id, worker_id, time.time(), status, details))

def get_heartbeat(task_id):
    """Get the last heartbeat for a task. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM heartbeats WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

def get_stale_heartbeats(stale_seconds=300):
    """Get tasks with heartbeats older than stale_seconds (default 5min)."""
    cutoff = time.time() - stale_seconds
    return query_db(
        "SELECT task_id, worker_id, timestamp, status FROM heartbeats WHERE timestamp < ? AND status = 'alive'",
        (cutoff,))

def kill_worker_process(task_id):
    """Kill a worker process by task ID. Returns True if killed."""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", task_id],
            capture_output=True, text=True, timeout=5
        )
        pids = result.stdout.strip().split('\n')
        killed = 0
        for pid in pids:
            if pid:
                try:
                    subprocess.run(["kill", "-9", pid], timeout=5)
                    killed += 1
                except:
                    pass
        return killed > 0
    except:
        return False

def is_worker_alive(task_id):
    """Check if a worker process is alive via OS process table."""
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", task_id],
            capture_output=True, text=True, timeout=5
        )
        return bool(result.stdout.strip())
    except:
        return False

def get_task_status(task_id):
    """Get task status from kanban DB."""
    try:
        import subprocess
        result = subprocess.run(
            ["hermes", "kanban", "show", task_id, "--json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout:
            import json
            return json.loads(result.stdout).get("task", {}).get("status", "?")
    except:
        pass
    return "?"

def abandon_task(task_id, reason):
    """Atomically abandon a task: kill process, mark heartbeat dead, ARCHIVE the card.

    2026-07-08 rewrite — the old version was abandon-in-name-only: it ran
    `hermes kanban unblock` (blocked -> READY!) / `reclaim`, so every janitor
    ABANDON actually REQUEUED the task. The three churning [RECOMPUTE] cards
    were "abandoned" dozens of times and respawned every cycle (25 -> 47 block
    events). It also shelled three `hermes` CLI subprocesses with timeout=10 —
    the CLI takes >10s to start under fleet load, so TimeoutExpired killed the
    whole janitor run mid-pass. Now: one direct kanban.db write (terminal
    status, archived event carrying the reason), no subprocesses.
    """
    killed = kill_worker_process(task_id)

    # Mark heartbeat as dead
    with get_db() as conn:
        conn.execute(
            "UPDATE heartbeats SET status = 'dead' WHERE task_id = ?",
            (task_id,))

    # Terminal archive, direct on kanban.db (short transaction, no CLI startup)
    kanban_path = os.path.join(_HERMES_MAIN, "kanban.db")
    try:
        k = sqlite3.connect(kanban_path, timeout=15)
        k.execute("PRAGMA busy_timeout=15000")
        now = time.time()
        cur = k.execute(
            "UPDATE tasks SET status='archived', completed_at=COALESCE(completed_at, ?) "
            "WHERE id = ? AND status IN ('blocked','ready','running','todo','triage')",
            (now, task_id))
        if cur.rowcount:
            k.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'archived', ?, ?)",
                (task_id, json.dumps({"by": "task_janitor", "reason": str(reason)[:300]}),
                 int(now)))
        k.commit()
        k.close()
    except Exception as e:
        print(f"  WARN abandon_task({task_id}): archive write failed: {e}")

    return killed
