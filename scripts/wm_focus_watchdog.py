#!/usr/bin/env python3
"""Watchdog: update WM focus to reflect the currently-running kanban task.

Runs as a cron job every minute. Reads running tasks from kanban.db,
picks the most recently heartbeated one, and sets it as WM focus.
"""
import json
import os
import re
import sqlite3
import urllib.request
import time

KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
WM_URL = "http://127.0.0.1:19876"


def get_running_task():
    """Get the most recently heartbeated running task."""
    try:
        conn = sqlite3.connect(KANBAN_DB, timeout=3)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        row = conn.execute("""
            SELECT t.id, t.title, t.assignee
            FROM tasks t
            WHERE t.status = 'running'
            ORDER BY (
                SELECT created_at FROM task_events
                WHERE task_id = t.id AND kind = 'heartbeat'
                ORDER BY id DESC LIMIT 1
            ) DESC
            LIMIT 1
        """).fetchone()
        conn.close()
        return row
    except Exception:
        return None


def set_focus(concept):
    """Set WM daemon focus."""
    try:
        data = json.dumps({"concept": concept}).encode()
        req = urllib.request.Request(
            WM_URL + "/focus",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


def get_current_focus():
    """Get current WM focus."""
    try:
        req = urllib.request.Request(
            WM_URL + "/status",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json"},
        )
        r = urllib.request.urlopen(req, timeout=2)
        return json.loads(r.read()).get("focus", "")
    except Exception:
        return ""


def main():
    task = get_running_task()
    if not task:
        # No running tasks — clear focus
        current = get_current_focus()
        if current:
            set_focus("")
        return

    task_id, title, assignee = task

    # Extract hypothesis from title
    m = re.search(r'exp_\d+:\s*(.+)', title)
    if m:
        concept = m.group(1).strip()[:80]
    else:
        concept = title[:80]

    # Only update if focus actually changed
    current = get_current_focus()
    if current == concept:
        return

    if set_focus(concept):
        print(f"[{time.strftime('%H:%M:%S')}] Focus updated: {concept[:60]}... ({assignee})")


if __name__ == "__main__":
    main()
