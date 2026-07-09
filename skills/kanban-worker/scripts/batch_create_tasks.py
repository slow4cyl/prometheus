#!/usr/bin/env python3
"""Batch-create Kanban tasks from a list of dicts.

Usage:
    python3 scripts/batch_create_tasks.py tasks.json
    python3 scripts/batch_create_tasks.py  # reads from stdin

Input format (JSON array of objects):
[
  {
    "title": "exp_1050: Real-time production guardrail",
    "assignee": "prometheus-worker-1",
    "body": "CONTEXT: ...\\nHYPOTHESIS: ...\\nMETHOD: ...\\nEXPECTED: ...\\nDELIVERABLE: ..."
  },
  ...
]

Output: one line per task with status (OK/FAIL) and the task ID on success.
"""

import subprocess
import json
import sys
import os


def create_task(title, assignee, body, timeout=30):
    """Create a single Kanban task via CLI. Returns (task_id, error)."""
    cmd = ["hermes", "kanban", "create", title, "--assignee", assignee, "--body", body]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            # Parse task ID from output like "Created t_xxxxxxxx (ready, ...)"
            import re
            m = re.search(r'(t_[a-f0-9]+)', result.stdout)
            task_id = m.group(1) if m else "unknown"
            return task_id, None
        else:
            return None, result.stderr.strip()[:200]
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)[:200]


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            tasks = json.load(f)
    else:
        tasks = json.load(sys.stdin)

    results = []
    for task in tasks:
        task_id, error = create_task(
            title=task["title"],
            assignee=task["assignee"],
            body=task["body"],
        )
        status = "OK" if task_id else "FAIL"
        short_title = task["title"][:55]
        if task_id:
            print(f"  [{status}] {task_id} {short_title}")
        else:
            print(f"  [{status}] {short_title} — {error}")
        results.append({"title": task["title"], "task_id": task_id, "error": error})

    ok = sum(1 for r in results if r["task_id"])
    fail = len(results) - ok
    print(f"\nCreated: {ok}/{len(results)} tasks ({fail} failed)")


if __name__ == "__main__":
    main()
