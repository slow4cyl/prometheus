#!/usr/bin/env python3
"""Director Cycle — one-pass strategic oversight."""
import json, os, sys, sqlite3, subprocess
from datetime import datetime, timezone

DB = os.path.expanduser("~/.hermes/kanban.db")
SELF_STATE = os.path.expanduser("~/.hermes/self_state.json")
REFILLER_SUMMARY = os.path.expanduser("~/.hermes/refiller_summary.json")

def get_running_tasks():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, title, assignee, started_at, created_at FROM tasks WHERE status='running'"
    ).fetchall()
    conn.close()
    return rows

def get_ready_count():
    conn = sqlite3.connect(DB)
    count = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='ready'").fetchone()[0]
    conn.close()
    return count

def get_blocked_tasks():
    conn = sqlite3.connect(DB)
    rows = conn.execute(
        "SELECT id, title, assignee FROM tasks WHERE status='blocked'"
    ).fetchall()
    # Fetch block reasons from task_events (if table exists)
    results = []
    has_task_events = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_events'"
    ).fetchone() is not None
    for tid, title, assignee in rows:
        reason = "unknown"
        if has_task_events:
            try:
                reason_row = conn.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND event_type='blocked' ORDER BY created_at DESC LIMIT 1",
                    (tid,)
                ).fetchone()
                reason = reason_row[0] if reason_row else "unknown"
            except:
                pass
        results.append((tid, title, assignee, reason))
    conn.close()
    return results

def check_synthesis_health():
    try:
        r = subprocess.run(
            ["python3", os.path.expanduser("~/.hermes/scripts/check_synthesis_health.py")],
            capture_output=True, text=True, timeout=60
        )
        return r.stdout.strip()
    except Exception as e:
        return f"Synthesis health check failed: {e}"

def main():
    now = datetime.now(timezone.utc).isoformat()
    print(f"=== Director Cycle — {now} ===\n")

    # 1. Running tasks
    running = get_running_tasks()
    print(f"[Running] {len(running)} tasks")
    for r in running:
        tid, title, assignee, started, created = r
        print(f"  {tid}: {title[:80]} | {assignee} | started={started}")

    # 2. Ready tasks
    ready = get_ready_count()
    print(f"\n[Ready] {ready} tasks in queue")

    # 3. Blocked tasks
    blocked = get_blocked_tasks()
    if blocked:
        print(f"\n[Blocked] {len(blocked)} tasks")
        for b in blocked[:10]:
            tid, title, assignee, reason = b
            print(f"  {tid}: {title[:60]} | {assignee} | reason={reason}")
        if len(blocked) > 10:
            print(f"  ... and {len(blocked) - 10} more")
    else:
        print("\n[Blocked] 0 tasks")

    # 4. Refiller summary (guard against empty/corrupt file from race condition)
    if os.path.exists(REFILLER_SUMMARY):
        try:
            with open(REFILLER_SUMMARY) as f:
                raw = f.read()
            if not raw.strip():
                print("\n[Refiller] Summary file empty (refiller write race)")
            else:
                ref = json.loads(raw)
                print(f"\n[Refiller] Last run: {ref.get('timestamp','?')}")
                print(f"  Ready: {ref.get('ready','?')}, Created: {ref.get('created','?')}, Running: {ref.get('running','?')}")
                print(f"  Velocity: {ref.get('velocity','?')} tasks/min, Entropy: {ref.get('entropy','?')}")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"\n[Refiller] Summary parse error (race condition): {e}")
    else:
        print("\n[Refiller] No summary found")

    # 5. Queue curation check
    try:
        with open(SELF_STATE) as f:
            ss = json.load(f)
        queue = ss.get("curiosity_queue", [])
        queue_size = len(queue)
    except:
        queue_size = -1

    print(f"\n[Queue] {queue_size} items in curiosity queue")
    if queue_size > 0 and queue_size < 15000:
        print("  Running queue_curator.py --dry-run...")
        try:
            r = subprocess.run(
                ["python3", os.path.expanduser("~/.hermes/scripts/queue_curator.py"), "--dry-run"],
                capture_output=True, text=True, timeout=120
            )
            # Show last 20 lines of output
            lines = r.stdout.strip().split('\n')
            for line in lines[-20:]:
                print(f"  {line}")
            if r.returncode != 0:
                print(f"  WARNING: curator exited with code {r.returncode}")
                if r.stderr:
                    print(f"  stderr: {r.stderr[-200:]}")
        except subprocess.TimeoutExpired:
            print("  CURATOR TIMED OUT — skipping curation this cycle")
        except Exception as e:
            print(f"  Curator error: {e}")
    elif queue_size >= 15000:
        print("  Queue >15K items — SKIPPING curation (timeout risk)")

    # 6. Synthesis health
    print(f"\n[Synthesis Health]")
    synth = check_synthesis_health()
    for line in synth.split('\n')[-15:]:
        print(f"  {line}")

    # 7. System summary
    print(f"\n=== Cycle Complete ===")
    print(f"  Running: {len(running)}, Ready: {ready}, Blocked: {len(blocked)}, Queue: {queue_size}")

if __name__ == "__main__":
    main()
