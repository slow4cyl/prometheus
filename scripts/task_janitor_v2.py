#!/usr/bin/env python3
"""
Task Janitor v2 — SQLite-backed postmortem for stuck/failed Kanban tasks.
Uses prometheus.db for task state instead of parsing JSON.
"""
import fcntl
from prometheus_paths import HERMES_HOME as _PP_HERMES_HOME
import json
import os
import re
import sqlite3
import subprocess
import sys
HERMES_HOME = _PP_HERMES_HOME
sys.path.insert(0, HERMES_HOME)
import time
from datetime import datetime, timezone
from pathlib import Path

from db_retry import get_db

DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")
AUDIT_LOG = os.path.join(HERMES_HOME, "self_audit.log")
WORKSPACES_BASE = os.path.join(HERMES_HOME, "kanban/workspaces")
LOCK_PATH = os.path.join(HERMES_HOME, ".task_janitor.lock")


from prometheus_db import get_heartbeat, kill_worker_process, abandon_task


# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default

def is_worker_alive(task_id):
    """Check if worker is alive via heartbeat (5min threshold) or OS process (fallback)."""
    try:
        hb = get_heartbeat(task_id)
        if hb:
            age = time.time() - hb.get("timestamp", 0)
            if age > 300:  # 5 minutes — stale heartbeat = dead
                return False
            return hb.get("status", "alive") == "alive"
    except Exception:
        pass
    # Fallback to OS check if no heartbeat recorded
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", task_id],
            capture_output=True, text=True, timeout=5
        )
        return bool(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False

# Canonical main hermes dir — derived from script location to avoid HOME override issues
_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default
# Resolve HERMES_HOME: use env var if set, but fall back to main dir if profile dir lacks infrastructure
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(os.path.join(_hermes_home_env, "prometheus_db.py")) else _HERMES_MAIN

STALE_MINUTES = 45
LONG_RUNNING_MINUTES = 60
MAX_RETRIES = 2

# Experiment-id token in a task title, e.g. "exp_1782685474000009412: [TRANSFER] ...".
# Mirrors db_reconciliation_monitor.EXP_ID_RE so both agree on what a result is.
_EXP_ID_RE = re.compile(r"(exp_[A-Za-z0-9_]+)")


def experiment_result_in_prometheus(title):
    """For an experiment task, has its worker_result actually reached prometheus?

    Returns True if the exp_id parsed from the title has a row in prometheus.db
    (worker_results OR experiments), False if it is an experiment task whose
    result never landed, and None if the title carries no exp_id (not an
    experiment task — the caller keeps its workspace-file heuristic then).

    Why this exists: analyze_workspace().has_results is true for ANY stray
    .json/summary file or /tmp/exp_*_output.log in the workspace — a half-written
    script or intermediate dump, NOT a completed result. Auto-completing an
    experiment task on that signal mints a "done, has results" run with no
    worker_result behind it, which db_reconciliation_monitor then flags as a lost
    experiment forever (first seen: t_493718b8, a [TRANSFER] task whose worker
    context-overflowed before it ever called write_worker_result.py). A real
    result lives in prometheus.db, so that is what we check."""
    m = _EXP_ID_RE.search(title or "")
    if not m:
        return None
    exp_id = m.group(1).rstrip("_")
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        try:
            hit = conn.execute(
                "SELECT 1 FROM worker_results WHERE experiment_id=? "
                "UNION ALL SELECT 1 FROM experiments WHERE id=? LIMIT 1",
                (exp_id, exp_id)).fetchone()
        finally:
            conn.close()
        return hit is not None
    except sqlite3.Error:
        # Can't prove absence — fail open to the old heuristic rather than
        # reclaim a task on a transient DB error.
        return None



def run_kanban(args):
    # 30s (from 10s): the CLI call is ~3s nominally but spikes past 10s under
    # kanban write churn (drain-mode task volume) — 2 of 6 janitor runs died
    # on the old timeout. Same philosophy as the DB busy_timeout fix: a
    # waiter must outlast the longest legitimate holder.
    result = subprocess.run(
        ["hermes", "kanban"] + args,
        capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip(), result.returncode


def get_tasks(status=None):
    args = ["list", "--json"]
    if status:
        args += ["--status", status]
    stdout, rc = run_kanban(args)
    if rc != 0 or not stdout:
        return []
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return []


def analyze_workspace(task_id):
    workspace = os.path.join(WORKSPACES_BASE, task_id)
    if not os.path.isdir(workspace):
        return {"exists": False, "status": "no_workspace"}

    files = [f for f in Path(workspace).glob("*") if not f.name.startswith(".")]
    if not files:
        return {"exists": True, "status": "empty", "files": []}

    now = time.time()
    newest_mtime = max(os.path.getmtime(f) for f in files)
    total_age_min = (now - newest_mtime) / 60

    report_files = [f.name for f in files if "REPORT" in f.name.upper() or "report" in f.name.lower()]

    # Also check /tmp for output files (some scripts write there via tee)
    tmp_outputs = []
    try:
        for f in os.listdir("/tmp"):
            if f.startswith("exp_") and f.endswith("_output.log"):
                tmp_path = os.path.join("/tmp", f)
                tmp_age = (time.time() - os.path.getmtime(tmp_path)) / 60
                if tmp_age < 60:
                    tmp_outputs.append({"name": f, "age_min": round(tmp_age, 1)})
    except OSError:
        pass
    result_files = [f.name for f in files if f.name.endswith(".json") and ("result" in f.name.lower() or "summary" in f.name.lower())]

    return {
        "exists": True,
        "status": "has_files",
        "file_count": len(files),
        "newest_age_min": round(total_age_min, 1),
        "has_report": len(report_files) > 0,
        "report_files": report_files,
        "has_results": len(result_files) > 0 or len(tmp_outputs) > 0,
        "files": [f.name for f in sorted(files, key=lambda x: -os.path.getmtime(x))[:10]],
    }


def count_reclaims(task_id):
    """Count reclaims from SQLite audit log."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM audit_log WHERE content LIKE ? AND entry_type = 'JANITOR'",
                (f"%{task_id}%",)
            ).fetchone()
            return row["n"]
    except sqlite3.Error:
        return 0


def count_blocked_events(task_id):
    """Count how many times a task has been blocked (by workers or janitor).
    Counts BOTH `blocked` (worker/goal-loop kanban_block) AND `gave_up` (the
    dispatcher circuit-breaker trip — the reaper's block path, which is the vast
    majority: a churning task shows dozens of `gave_up` and zero `blocked`). The
    old query counted only `blocked`, so the abandon-guard below was blind to
    reaper-blocked tasks and the janitor re-queued them forever — dozens of
    respawn→protocol-violation→gave_up→unblock cycles per task."""
    try:
        kanban_db = os.path.join(HERMES_HOME, "kanban.db")
        conn = sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT COUNT(*) as n FROM task_events WHERE task_id = ? "
            "AND kind IN ('blocked', 'gave_up')",
            (task_id,)
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


# Max times a task can be blocked before janitor archives/abandons it
MAX_BLOCKED_EVENTS = 3


def analyze_task(task):
    task_id = task["id"]
    title = task.get("title", "?")
    status = task.get("status", "?")
    started = task.get("started_at")
    created = task.get("created_at")

    analysis = {
        "task_id": task_id,
        "title": title,
        "status": status,
        "decision": "SKIP",
        "reason": "",
        "workspace": None,
    }

    ws = analyze_workspace(task_id)
    analysis["workspace"] = ws

    now = time.time()
    task_age_min = ((now - started) / 60) if started else ((now - created) / 60) if created else 0
    analysis["age_min"] = round(task_age_min, 1)

    ws_age_min = ws.get("newest_age_min", 0) if ws.get("exists") else None
    reclaims = count_reclaims(task_id)
    analysis["reclaims"] = reclaims

    if status == "blocked":
        blocked_count = count_blocked_events(task_id)
        analysis["blocked_count"] = blocked_count
        if ws.get("has_report"):
            analysis["decision"] = "AUTO-COMPLETE"
            analysis["reason"] = "Report exists but task is blocked."
        elif blocked_count >= MAX_BLOCKED_EVENTS:
            analysis["decision"] = "ABANDON"
            analysis["reason"] = f"Blocked {blocked_count}x (>{MAX_BLOCKED_EVENTS} reblocks). Likely garbage/recycling artifact."
        elif not ws.get("exists"):
            analysis["decision"] = "REQUEUE"
            analysis["reason"] = "Blocked with no workspace."
        elif task_age_min > 60:
            if reclaims >= MAX_RETRIES:
                analysis["decision"] = "ABANDON"
                analysis["reason"] = f"Task age {task_age_min:.0f}min, reclaimed {reclaims}x."
            else:
                analysis["decision"] = "RECLAIM"
                analysis["reason"] = f"Task age {task_age_min:.0f}min (>60min)."
        else:
            analysis["decision"] = "SKIP"
            analysis["reason"] = f"Blocked but age {task_age_min:.0f}min (<60min)."

    elif status == "running":
        if ws.get("has_report") and task_age_min < 60:
            analysis["decision"] = "AUTO-COMPLETE"
            analysis["reason"] = "Report exists and task is recent."
        elif task_age_min > LONG_RUNNING_MINUTES:
            prom_result = experiment_result_in_prometheus(title)
            if prom_result is True:
                analysis["decision"] = "AUTO-COMPLETE"
                analysis["reason"] = f"Long-running ({task_age_min:.0f}min), result in prometheus."
            elif prom_result is False:
                # Experiment task whose result never reached prometheus. A stray
                # workspace file is NOT a result — completing here mints a
                # reconciliation ghost. Give a live worker room; otherwise retry,
                # then abandon (never auto-complete a resultless experiment).
                if is_worker_alive(task_id):
                    analysis["decision"] = "SKIP"
                    analysis["reason"] = f"Long-running ({task_age_min:.0f}min), no prometheus result yet but worker alive."
                elif reclaims >= MAX_RETRIES:
                    analysis["decision"] = "ABANDON"
                    analysis["reason"] = f"Long-running ({task_age_min:.0f}min), no prometheus result after {reclaims} reclaims."
                else:
                    analysis["decision"] = "RECLAIM"
                    analysis["reason"] = f"Long-running ({task_age_min:.0f}min), no prometheus result yet."
            elif ws.get("has_results"):
                analysis["decision"] = "AUTO-COMPLETE"
                analysis["reason"] = f"Long-running ({task_age_min:.0f}min) but has results."
            elif reclaims >= MAX_RETRIES:
                analysis["decision"] = "ABANDON"
                analysis["reason"] = f"Long-running ({task_age_min:.0f}min), reclaimed {reclaims}x."
            else:
                analysis["decision"] = "RECLAIM"
                analysis["reason"] = f"Long-running ({task_age_min:.0f}min)."
        elif not ws.get("exists") and task_age_min > 10:
            analysis["decision"] = "RECLAIM"
            analysis["reason"] = f"Running {task_age_min:.0f}min but no workspace."
        else:
            analysis["decision"] = "SKIP"
            analysis["reason"] = f"Running {task_age_min:.0f}min, workspace {'active' if ws.get('exists') else 'absent'}."

    return analysis


def execute_decision(analysis):
    task_id = analysis["task_id"]
    decision = analysis["decision"]
    reason = analysis["reason"]
    status = analysis["status"]

    if decision == "SKIP":
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if decision == "AUTO-COMPLETE":
        ws = analysis.get("workspace", {})
        summary = reason
        if ws.get("report_files"):
            report_path = os.path.join(WORKSPACES_BASE, task_id, ws["report_files"][0])
            try:
                with open(report_path) as f:
                    summary = f.read()[:2000]
            except OSError:
                pass
        run_kanban(["complete", task_id, "--result", f"JANITOR: {reason}", "--summary", summary[:1000]])
        log_to_db(ts, task_id, decision, reason, "completed")
        return True

    elif decision in ("RECLAIM", "REQUEUE"):
        if status == "blocked":
            run_kanban(["unblock", task_id, "--reason", f"Janitor: {reason}"])
        else:
            run_kanban(["reclaim", task_id])
        log_to_db(ts, task_id, decision, reason, "reclaimed")
        return True

    elif decision == "ABANDON":
        # Atomic abandon: kill process + mark dead + update kanban
        if is_worker_alive(task_id):
            log_to_db(ts, task_id, "SKIP", f"Worker alive (heartbeat or process) for {task_id}", "skipped")
            return False
        killed = abandon_task(task_id, reason)
        log_to_db(ts, task_id, decision, f"{reason} (process_killed={killed})", "abandoned")
        return True

    return False


def log_to_db(ts, task_id, decision, reason, action):
    """Log postmortem to SQLite audit log."""
    try:
        ts_epoch = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        with get_db() as conn:
            conn.execute(
                "INSERT INTO audit_log (timestamp, entry_type, content) VALUES (?, 'JANITOR', ?)",
                (ts_epoch, f"JANITOR: {task_id} — {decision}\n  REASON: {reason}\n  ACTION: {action}")
            )
    except Exception:
        pass
    # Also log to traditional audit log for backward compat
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(f"[{ts}] JANITOR: {task_id} — {decision}\n  REASON: {reason}\n  ACTION: {action}\n")
    except OSError:
        pass


# Worker-spawned tasks (kanban_create from inside a worker session) bypass
# task_refiller.get_task_priority and land at priority 0 — the residual tier,
# which under the perpetual p1-p4 refill NEVER dispatches (5 such tasks sat
# unworked 2026-07-04). This pass re-tiers ready p0/NULL tasks older than 10
# minutes whose TITLE matches a known lane pattern (same text rules as
# get_task_priority); unrecognized titles stay at 0 — the residual tier
# remains real for genuinely unclassifiable work.
_PRIORITY_PATTERNS = (
    ("[BENCH3", 4),
    ("[CANDIDATE-RETEST]", 3),
    ("SYNTHESIZE ", 2), ("SYNTHESIS:", 2), ("[SYNTHESIS", 2),
    ("[COMPRESSION", 2), ("COMPRESSION-", 2),
    ("[TRANSFER", 1), ("[BOUNDARY]", 1), ("[LIT-RESIDUE]", 1),
)


def classify_title_priority(title):
    t = (title or "").lstrip().upper()
    # strip a leading [REFILLER]/exp_NN: preamble so the lane tag is visible
    t = re.sub(r"^\[REFILLER\]\s*", "", t)
    t = re.sub(r"^EXP_[A-Z0-9_]+:\s*", "", t)
    for prefix, prio in _PRIORITY_PATTERNS:
        if t.startswith(prefix):
            return prio
    return None


def normalize_spawned_priorities():
    """Bump ready p0/NULL tasks with a classifiable title to their lane tier."""
    kanban = os.path.join(HERMES_HOME, "kanban.db")
    try:
        conn = sqlite3.connect(kanban, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        rows = conn.execute(
            "SELECT id, title FROM tasks WHERE status='ready' "
            "AND COALESCE(priority, 0) = 0 AND created_at < ?",
            (int(time.time()) - 600,)).fetchall()
        bumped = 0
        for tid, title in rows:
            prio = classify_title_priority(title)
            if prio:
                conn.execute("UPDATE tasks SET priority=? WHERE id=? AND status='ready'",
                             (prio, tid))
                print(f"  PRIORITY {tid}: 0 -> {prio}  {(title or '')[:60]}")
                bumped += 1
        # Decomposition children: the decomposer created them WITHOUT a priority
        # (p0), so they starved behind perpetual p1-p4 refill — the whole family
        # (children in todo, root waiting on them) parked forever, visible only
        # as "stuck in todo". Inherit the ROOT's priority via the created-event
        # from_decompose_of pointer. (Source fix lands in
        # kanban_db.decompose_triage_task at the next gateway restart; this
        # heals strays created before/without it.)
        dec_rows = conn.execute(
            """SELECT t.id, t.status,
                      json_extract(e.payload, '$.from_decompose_of') root_id
               FROM tasks t
               JOIN task_events e ON e.task_id = t.id AND e.kind = 'created'
               WHERE t.status IN ('ready', 'todo') AND COALESCE(t.priority, 0) = 0
                 AND e.payload LIKE '%from_decompose_of%'
                 AND t.created_at < ?""",
            (int(time.time()) - 600,)).fetchall()
        for tid, tstatus, root_id in dec_rows:
            if not root_id:
                continue
            row = conn.execute("SELECT priority FROM tasks WHERE id = ?", (root_id,)).fetchone()
            root_prio = (row[0] if row and row[0] is not None else 0)
            if root_prio > 0:
                conn.execute(
                    "UPDATE tasks SET priority=? WHERE id=? AND COALESCE(priority,0)=0",
                    (root_prio, tid))
                print(f"  PRIORITY {tid} [{tstatus}]: 0 -> {root_prio} (inherited from decompose root {root_id})")
                bumped += 1
        if bumped:
            conn.commit()
            print(f"Normalized {bumped} worker-spawned task priorities\n")
        conn.close()
    except Exception as e:
        print(f"  priority normalization skipped: {e}")


def normalize_orphaned_assignees():
    """Reassign tasks stuck on a non-existent profile to 'default' (self-heal).

    Tasks historically stranded on invented worker names from two sources —
    workers guessing 'prometheus-worker-N' in kanban_create, and the dispatcher
    spilling per-profile-cap overflow onto invented 'prometheus-worker-N' names
    (60k+ reassignments to profiles that don't exist). Both are fixed at the
    source; this is the backstop that guarantees no task orphans on a phantom
    assignee regardless of source or code-reload timing. Any ready/todo/blocked/
    triage task whose assignee is neither 'default' nor an installed profile
    under ~/.hermes/profiles/ is reassigned to 'default' so it dispatches."""
    kanban = os.path.join(HERMES_HOME, "kanban.db")
    try:
        profiles_dir = os.path.join(HERMES_HOME, "profiles")
        valid = {"default"}
        if os.path.isdir(profiles_dir):
            valid |= {d.lower() for d in os.listdir(profiles_dir)
                      if os.path.isdir(os.path.join(profiles_dir, d))}
        conn = sqlite3.connect(kanban, timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
        rows = conn.execute(
            "SELECT id, assignee, title FROM tasks "
            "WHERE status IN ('ready','todo','blocked','triage') "
            "AND assignee IS NOT NULL").fetchall()
        fixed = 0
        for tid, assignee, title in rows:
            if (assignee or "").strip().lower() in valid:
                continue
            conn.execute("UPDATE tasks SET assignee='default' WHERE id=? "
                         "AND status IN ('ready','todo','blocked','triage')", (tid,))
            print(f"  ASSIGNEE {tid}: {assignee!r} -> 'default'  {(title or '')[:50]}")
            fixed += 1
        if fixed:
            conn.commit()
            print(f"Rescued {fixed} orphaned-assignee task(s) -> 'default'\n")
        conn.close()
    except Exception as e:
        print(f"  assignee normalization skipped: {e}")


def main():
    print(f"=== Task Janitor v2: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    normalize_spawned_priorities()
    normalize_orphaned_assignees()

    all_tasks = []
    for status in ["running", "blocked", "ready"]:
        all_tasks.extend(get_tasks(status))

    if not all_tasks:
        print("No tasks found.")
        return

    print(f"Found {len(all_tasks)} tasks\n")

    actions = []
    skipped = []

    for task in all_tasks:
        analysis = analyze_task(task)
        if analysis["decision"] == "SKIP":
            skipped.append(f"  SKIP  {analysis['task_id']} [{analysis['status']}] {analysis['title'][:50]}")
            skipped.append(f"         → {analysis['reason']}")
        else:
            executed = execute_decision(analysis)
            icon = "✓" if executed else "✗"
            actions.append(f"  {icon} {analysis['decision']:15s} {analysis['task_id']} [{analysis['status']}] {analysis['title'][:50]}")
            actions.append(f"         → {analysis['reason']}")

    if actions:
        print("ACTIONS TAKEN:")
        for line in actions:
            print(line)
        print()

    if skipped:
        print("SKIPPED:")
        for line in skipped:
            print(line)
        print()

    action_count = sum(1 for l in actions if l.strip().startswith("✓"))
    print(f"Summary: {action_count} actions, {len(skipped)//2} skipped")

    # Print DB metrics. 2026-07-12: the old "unsynthesized" count checked the
    # frozen subtopics table and matched ~every completed experiment — a fossil
    # number printed every 10 minutes. Unclaimed = completed results not yet
    # linked into the claim graph (the live meaning of "not synthesized").
    try:
        conn = get_db()
        unclaimed = conn.execute(
            "SELECT COUNT(*) as n FROM experiments e WHERE e.status = 'completed' "
            "AND NOT EXISTS (SELECT 1 FROM claim_evidence ce WHERE ce.experiment_id = e.id)"
        ).fetchone()["n"]
        total_exp = conn.execute("SELECT COUNT(*) as n FROM experiments").fetchone()["n"]
        conn.close()
        print(f"\nDB: {total_exp} experiments, {unclaimed} unclaimed (no claim_evidence link)")
    except Exception:
        pass


if __name__ == "__main__":
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another instance running, skipping.")
        sys.exit(0)
    try:
        main()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
