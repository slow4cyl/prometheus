#!/usr/bin/env python3
"""Self-test for the prometheus-guard worker-result-written completion gate.

Runnable with the hermes-agent venv python:

    ~/.hermes/hermes-agent/venv/bin/python \
        ~/.hermes/plugins/prometheus-guard/test_completion_gate.py

Loads ``__init__.py.staged`` when present (pre-promotion self-test),
otherwise the live ``__init__.py``. All DB fixtures are fabricated temp
sqlite files — the test never touches the live kanban.db / prometheus.db
(the module's kanban path is redirected via HERMES_KANBAN_DB and its
prometheus path via the module-level ``_PROMETHEUS_DB`` constant).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sqlite3
import sys
import tempfile

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_module():
    staged = os.path.join(PLUGIN_DIR, "__init__.py.staged")
    live = os.path.join(PLUGIN_DIR, "__init__.py")
    path = staged if os.path.exists(staged) else live
    loader = importlib.machinery.SourceFileLoader(
        "prometheus_guard_under_test", path
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod, path


def _make_kanban_db(path: str, tasks) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "body TEXT, status TEXT)"
    )
    conn.executemany(
        "INSERT INTO tasks (id, title, body, status) VALUES (?, ?, ?, 'in_progress')",
        tasks,
    )
    conn.commit()
    conn.close()


def _make_prometheus_db(path: str, results) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE worker_results (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "experiment_id TEXT NOT NULL, kanban_task_id TEXT, key_finding TEXT)"
    )
    conn.executemany(
        "INSERT INTO worker_results (experiment_id, kanban_task_id, key_finding) "
        "VALUES (?, ?, 'test finding')",
        results,
    )
    conn.commit()
    conn.close()


def main() -> int:
    mod, mod_path = _load_module()
    print(f"module under test: {mod_path}")

    failures = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="pg-gate-test-") as td:
        kdb = os.path.join(td, "kanban.db")
        pdb = os.path.join(td, "prometheus.db")
        _make_kanban_db(
            kdb,
            [
                ("task-exp-noresult", "exp_9001: probe something", "body text"),
                ("task-exp-hasresult", "exp_9002: probe something else", "body"),
                ("task-transfer-noresult", "[TRANSFER] retest exp_9001 in domain X",
                 "source: exp_9001"),
                ("task-transfer-hasresult", "[TRANSFER] retest exp_9002 in domain Y",
                 "source: exp_9002"),
                ("task-synth", "synthesis: weekly digest", "body"),
            ],
        )
        _make_prometheus_db(
            pdb,
            [
                ("exp_9002", "task-exp-hasresult"),
                ("exp_9002_t1", "task-transfer-hasresult"),
            ],
        )

        # Point the module at the fabricated DBs.
        os.environ["HERMES_KANBAN_DB"] = kdb
        os.environ["HERMES_KANBAN_TASK"] = "task-exp-noresult"
        mod._PROMETHEUS_DB = pdb

        hook = mod._pre_tool_call

        print("\n(1) blocks a resultless exp_ completion")
        r = hook(tool_name="kanban_complete", args={"task_id": "task-exp-noresult"})
        check("returns a block action", isinstance(r, dict) and r.get("action") == "block",
              f"got {r!r}")
        check(
            "message matches the fork text",
            isinstance(r, dict)
            and "no worker_results entry found for task task-exp-noresult" in r.get("message", "")
            and "You MUST call write_worker_result.py BEFORE kanban_complete" in r.get("message", "")
            and "write_worker_result.py --experiment <id>" in r.get("message", "").replace(
                "~/.hermes/scripts/", ""
            ),
            f"got {r!r}",
        )

        print("\n(1b) blocks a resultless [TRANSFER] completion")
        os.environ["HERMES_KANBAN_TASK"] = "task-transfer-noresult"
        r = hook(tool_name="kanban_complete", args={"task_id": "task-transfer-noresult"})
        check("returns a block action", isinstance(r, dict) and r.get("action") == "block",
              f"got {r!r}")

        print("\n(2) allows when a worker_results row exists")
        os.environ["HERMES_KANBAN_TASK"] = "task-exp-hasresult"
        r = hook(tool_name="kanban_complete", args={"task_id": "task-exp-hasresult"})
        check("exp_ with experiment_id row -> None", r is None, f"got {r!r}")
        os.environ["HERMES_KANBAN_TASK"] = "task-transfer-hasresult"
        r = hook(tool_name="kanban_complete", args={"task_id": "task-transfer-hasresult"})
        check("[TRANSFER] with kanban_task_id row -> None", r is None, f"got {r!r}")

        print("\n(3) allows non-matching titles")
        os.environ["HERMES_KANBAN_TASK"] = "task-synth"
        r = hook(tool_name="kanban_complete", args={"task_id": "task-synth"})
        check("synthesis title -> None", r is None, f"got {r!r}")

        print("\n(3b) no HERMES_KANBAN_TASK (orchestrator) -> None even for exp_")
        del os.environ["HERMES_KANBAN_TASK"]
        r = hook(tool_name="kanban_complete", args={"task_id": "task-exp-noresult"})
        check("orchestrator context -> None", r is None, f"got {r!r}")
        os.environ["HERMES_KANBAN_TASK"] = "task-exp-noresult"

        print("\n(4) fails open when DBs are absent")
        os.environ["HERMES_KANBAN_DB"] = os.path.join(td, "nope-kanban.db")
        r = hook(tool_name="kanban_complete", args={"task_id": "task-exp-noresult"})
        check("kanban.db absent -> None", r is None, f"got {r!r}")
        os.environ["HERMES_KANBAN_DB"] = kdb
        mod._PROMETHEUS_DB = os.path.join(td, "nope-prometheus.db")
        r = hook(tool_name="kanban_complete", args={"task_id": "task-exp-noresult"})
        check("prometheus.db absent -> None", r is None, f"got {r!r}")
        mod._PROMETHEUS_DB = pdb

        print("\n(4b) fails open on schema drift (worker_results table missing)")
        empty_pdb = os.path.join(td, "empty-prometheus.db")
        sqlite3.connect(empty_pdb).close()
        mod._PROMETHEUS_DB = empty_pdb
        r = hook(tool_name="kanban_complete", args={"task_id": "task-exp-noresult"})
        check("no worker_results table -> None", r is None, f"got {r!r}")
        mod._PROMETHEUS_DB = pdb

        print("\n(5) kanban_block path untouched")
        r = hook(tool_name="kanban_block", args={"task_id": "task-exp-noresult",
                                                 "reason": ""})
        check("empty reason -> None (kanban_block's own error fires)", r is None,
              f"got {r!r}")
        r = hook(tool_name="kanban_block",
                 args={"task_id": "task-exp-noresult",
                       "reason": "blocked: database is locked"})
        check("genuine infra reason -> None", r is None, f"got {r!r}")
        r = hook(tool_name="some_other_tool", args={})
        check("unrelated tool -> None", r is None, f"got {r!r}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
