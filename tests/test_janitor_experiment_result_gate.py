"""Regression pin for the janitor's "resultless experiment" ghost.

Root cause (incident 2026-07-13, task t_493718b8): a [TRANSFER] worker
context-overflowed before it ever called write_worker_result.py, leaving only a
half-written script in its workspace. analyze_workspace().has_results is true for
ANY stray .json/summary file, so the janitor AUTO-COMPLETEd the task with
"Long-running (76min) but has results" — a done+summary run with NO worker_result
behind it. db_reconciliation_monitor then flagged it as a lost experiment forever.

The fix: for experiment tasks (title carries an exp_ token) the janitor trusts
prometheus.db, not workspace scratch files. These tests pin that a resultless
experiment is retried/abandoned (never auto-completed) while non-experiment tasks
keep the workspace-file heuristic and real results still auto-complete.
"""
import os
import sqlite3
import time

import task_janitor_v2 as j


def _prom_db(tmp_path, exp_ids_with_results=()):
    """Build a minimal prometheus.db with the two tables the gate reads."""
    p = os.path.join(str(tmp_path), "prometheus.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE worker_results (experiment_id TEXT)")
    conn.execute("CREATE TABLE experiments (id TEXT)")
    for eid in exp_ids_with_results:
        conn.execute("INSERT INTO worker_results (experiment_id) VALUES (?)", (eid,))
    conn.commit()
    conn.close()
    return p


def test_result_lookup_true_false_none(tmp_path, monkeypatch):
    monkeypatch.setattr(j, "DB_PATH", _prom_db(tmp_path, ["exp_AAA"]))
    assert j.experiment_result_in_prometheus("exp_AAA: [TRANSFER] foo") is True
    assert j.experiment_result_in_prometheus("exp_BBB: [TRANSFER] foo") is False
    # No exp_ token → not an experiment task → None (caller keeps its heuristic).
    assert j.experiment_result_in_prometheus("Synthesize cluster overlap") is None


def _running_task(exp_title):
    return {
        "id": "t_ghost",
        "title": exp_title,
        "status": "running",
        "started_at": time.time() - (j.LONG_RUNNING_MINUTES + 20) * 60,
        "created_at": time.time() - (j.LONG_RUNNING_MINUTES + 20) * 60,
    }


def test_resultless_experiment_is_not_autocompleted(tmp_path, monkeypatch):
    # Workspace has a stray file (the trap), but NO worker_result in prometheus.
    monkeypatch.setattr(j, "DB_PATH", _prom_db(tmp_path, []))
    monkeypatch.setattr(j, "analyze_workspace", lambda tid: {
        "exists": True, "has_results": True, "has_report": False,
        "report_files": [], "newest_age_min": 5, "files": [],
    })
    monkeypatch.setattr(j, "count_reclaims", lambda tid: 0)
    monkeypatch.setattr(j, "is_worker_alive", lambda tid: False)
    a = j.analyze_task(_running_task("exp_BBB: [TRANSFER] paleoanthropology"))
    assert a["decision"] == "RECLAIM", a
    assert "no prometheus result" in a["reason"]


def test_resultless_experiment_abandoned_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(j, "DB_PATH", _prom_db(tmp_path, []))
    monkeypatch.setattr(j, "analyze_workspace", lambda tid: {
        "exists": True, "has_results": True, "has_report": False,
        "report_files": [], "newest_age_min": 5, "files": [],
    })
    monkeypatch.setattr(j, "count_reclaims", lambda tid: j.MAX_RETRIES)
    monkeypatch.setattr(j, "is_worker_alive", lambda tid: False)
    a = j.analyze_task(_running_task("exp_BBB: [TRANSFER] paleoanthropology"))
    assert a["decision"] == "ABANDON", a


def test_live_worker_gets_room(tmp_path, monkeypatch):
    monkeypatch.setattr(j, "DB_PATH", _prom_db(tmp_path, []))
    monkeypatch.setattr(j, "analyze_workspace", lambda tid: {
        "exists": True, "has_results": True, "has_report": False,
        "report_files": [], "newest_age_min": 5, "files": [],
    })
    monkeypatch.setattr(j, "count_reclaims", lambda tid: 0)
    monkeypatch.setattr(j, "is_worker_alive", lambda tid: True)
    a = j.analyze_task(_running_task("exp_BBB: [TRANSFER] paleoanthropology"))
    assert a["decision"] == "SKIP", a


def test_real_result_autocompletes(tmp_path, monkeypatch):
    monkeypatch.setattr(j, "DB_PATH", _prom_db(tmp_path, ["exp_CCC"]))
    monkeypatch.setattr(j, "analyze_workspace", lambda tid: {
        "exists": True, "has_results": False, "has_report": False,
        "report_files": [], "newest_age_min": 5, "files": [],
    })
    monkeypatch.setattr(j, "count_reclaims", lambda tid: 0)
    monkeypatch.setattr(j, "is_worker_alive", lambda tid: False)
    a = j.analyze_task(_running_task("exp_CCC: [TRANSFER] paleoanthropology"))
    assert a["decision"] == "AUTO-COMPLETE", a
    assert "result in prometheus" in a["reason"]


def test_non_experiment_task_keeps_workspace_heuristic(tmp_path, monkeypatch):
    # No exp_ token → the workspace-file heuristic still auto-completes.
    monkeypatch.setattr(j, "DB_PATH", _prom_db(tmp_path, []))
    monkeypatch.setattr(j, "analyze_workspace", lambda tid: {
        "exists": True, "has_results": True, "has_report": False,
        "report_files": [], "newest_age_min": 5, "files": [],
    })
    monkeypatch.setattr(j, "count_reclaims", lambda tid: 0)
    monkeypatch.setattr(j, "is_worker_alive", lambda tid: False)
    a = j.analyze_task(_running_task("Synthesize cluster overlap report"))
    assert a["decision"] == "AUTO-COMPLETE", a
    assert a["reason"].endswith("but has results.")
