#!/usr/bin/env python3
"""artifact_preserve.py — persist experiment artifacts before workspaces vanish.

Why this exists (2026-07-01): scratch workspaces are torn down essentially at
task completion (`hermes kanban gc` removes archived-task workspaces with NO
preservation, and the janitor archives done tasks within minutes). Measured:
of 3,299 tasks completed in 24h, only 44 had anything preserved. The
preserving GC (scripts/workspace_gc.py) never wins that race. Result: no
experiment above CANDIDATE was re-runnable or auditable.

Fix: preserve at the only race-free moment — while the WORKER's own process
is still alive. write_worker_result.py calls preserve_for_task() as part of
the result write (workspace guaranteed to exist); apply_worker_results.py
calls finalize_manifest() on quality-gate pass to freeze the intake verdict
alongside the files.

Layout (matches what verify_artifacts() already deep-searches):
    ~/.hermes/artifacts/<kanban_task_id>/<files...>
    ~/.hermes/artifacts/<kanban_task_id>/manifest.json

Falls back to <experiment_id> as the directory name when no task id is known.
All functions are best-effort and must never raise into the caller: workers
and the intake cron depend on the write path staying up.
"""
import json
import os
import shutil
import time

EVIDENCE_EXTS = {'.py', '.json', '.csv', '.md', '.txt', '.yaml', '.yml',
                 '.toml', '.cfg', '.conf'}
MAX_FILE_BYTES = 2 * 1024 * 1024      # per-file cap: scripts/results are KB-sized
MAX_TOTAL_BYTES = 10 * 1024 * 1024    # per-task cap
MANIFEST_NAME = "manifest.json"


def _hermes_home():
    env = os.environ.get("HERMES_HOME", "")
    if env and os.path.isdir(env):
        return env
    return os.path.join(os.path.expanduser("~"), ".hermes")


def artifacts_dir(experiment_id=None, kanban_task_id=None, create=False):
    """Directory for this task/experiment's preserved artifacts.

    Prefers an EXISTING dir under either key (so write-time and apply-time
    calls converge on one location), else the task id, else the experiment id.
    """
    root = os.path.join(_hermes_home(), "artifacts")
    candidates = [c for c in (kanban_task_id, experiment_id) if c]
    if not candidates:
        return None
    for cand in candidates:
        d = os.path.join(root, str(cand))
        if os.path.isdir(d):
            return d
    d = os.path.join(root, str(candidates[0]))
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def _copy_capped(src, dest_dir, copied, skipped, budget):
    """Copy src into dest_dir under the size budget. Returns remaining budget."""
    try:
        size = os.path.getsize(src)
    except OSError:
        return budget
    name = os.path.basename(src)
    dest = os.path.join(dest_dir, name)
    if size == 0:
        return budget
    if size > MAX_FILE_BYTES or size > budget:
        skipped.append({"file": name, "bytes": size, "reason": "too_large"})
        return budget
    if os.path.exists(dest) and os.path.getsize(dest) == size:
        return budget  # already preserved (idempotent)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append({"file": name, "bytes": size})
        return budget - size
    except (OSError, shutil.Error):
        return budget


def preserve_for_task(files_list, experiment_id, kanban_task_id,
                      workspace_hint=None):
    """Copy the worker's evidence files to the persistent artifacts dir.

    Sources, in order:
      1. the task's scratch workspace (kanban/workspaces/<task_id>/), incl.
         one level of subdirectories — catches files the worker didn't list
      2. the CWD if it is itself a scratch workspace (worker runs inside it)
      3. explicitly listed files that resolve as absolute or CWD-relative

    Returns a summary dict (also merged into the manifest). Never raises.
    """
    summary = {"copied": [], "skipped": [], "sources": []}
    try:
        dest = artifacts_dir(experiment_id, kanban_task_id, create=False)
        if dest is None:
            return summary
        copied, skipped = summary["copied"], summary["skipped"]
        budget = MAX_TOTAL_BYTES

        ws_root = os.path.join(_hermes_home(), "kanban", "workspaces")
        sweep_dirs = []
        if kanban_task_id:
            sweep_dirs.append(os.path.join(ws_root, str(kanban_task_id)))
        if workspace_hint:
            sweep_dirs.append(workspace_hint)
        cwd = os.getcwd()
        if os.path.abspath(cwd).startswith(os.path.abspath(ws_root) + os.sep):
            sweep_dirs.append(cwd)

        seen = set()
        for d in sweep_dirs:
            d = os.path.abspath(d)
            if d in seen or not os.path.isdir(d):
                continue
            seen.add(d)
            summary["sources"].append(d)
            try:
                for entry in sorted(os.listdir(d)):
                    p = os.path.join(d, entry)
                    if os.path.isfile(p):
                        if os.path.splitext(entry)[1].lower() in EVIDENCE_EXTS:
                            budget = _copy_capped(p, dest, copied, skipped, budget)
                    elif os.path.isdir(p):
                        # one level deep, prefixed to avoid collisions
                        for sub in sorted(os.listdir(p)):
                            sp = os.path.join(p, sub)
                            if (os.path.isfile(sp) and
                                    os.path.splitext(sub)[1].lower() in EVIDENCE_EXTS):
                                try:
                                    size = os.path.getsize(sp)
                                except OSError:
                                    continue
                                if 0 < size <= min(MAX_FILE_BYTES, budget):
                                    pref = os.path.join(dest, f"{entry}__{sub}")
                                    if not (os.path.exists(pref) and
                                            os.path.getsize(pref) == size):
                                        try:
                                            os.makedirs(dest, exist_ok=True)
                                            shutil.copy2(sp, pref)
                                            copied.append({"file": f"{entry}__{sub}",
                                                           "bytes": size})
                                            budget -= size
                                        except (OSError, shutil.Error):
                                            pass
                                elif size > 0:
                                    skipped.append({"file": f"{entry}__{sub}",
                                                    "bytes": size,
                                                    "reason": "too_large"})
            except OSError:
                continue

        # explicitly listed files that resolve outside the swept dirs
        for fname in (files_list or []):
            fname = str(fname).strip()
            if not fname:
                continue
            for cand in ((fname,) if os.path.isabs(fname)
                         else (os.path.join(cwd, fname), fname)):
                if os.path.isfile(cand):
                    if os.path.splitext(cand)[1].lower() in EVIDENCE_EXTS:
                        budget = _copy_capped(cand, dest, copied, skipped, budget)
                    break

        if copied or skipped:
            update_manifest(experiment_id, kanban_task_id, {
                "experiment_id": experiment_id,
                "kanban_task_id": kanban_task_id,
                "preserved_at": time.time(),
                "preserve": summary,
            })
    except Exception:
        pass  # never break the worker's write path
    return summary


def update_manifest(experiment_id, kanban_task_id, fields):
    """Merge fields into artifacts/<id>/manifest.json. Never raises."""
    try:
        dest = artifacts_dir(experiment_id, kanban_task_id, create=True)
        if dest is None:
            return None
        path = os.path.join(dest, MANIFEST_NAME)
        data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f) or {}
            except (json.JSONDecodeError, OSError):
                data = {}
        data.update(fields)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, default=str)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def finalize_manifest(experiment_id, kanban_task_id, intake_fields):
    """Called by apply_worker_results on quality-gate pass: freeze the intake
    verdict next to the preserved files, and sweep the workspace once more in
    case the result-write hook missed it (e.g. bridge-recovered results).
    Never raises."""
    try:
        ws = (os.path.join(_hermes_home(), "kanban", "workspaces",
                           str(kanban_task_id)) if kanban_task_id else None)
        if ws and os.path.isdir(ws):
            preserve_for_task([], experiment_id, kanban_task_id,
                              workspace_hint=ws)
        fields = dict(intake_fields or {})
        fields.setdefault("experiment_id", experiment_id)
        fields.setdefault("kanban_task_id", kanban_task_id)
        fields["intake_finalized_at"] = time.time()
        return update_manifest(experiment_id, kanban_task_id, fields)
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    # smoke test: python3 artifact_preserve.py <exp_id> <task_id> [files...]
    exp, task = sys.argv[1], sys.argv[2]
    print(json.dumps(preserve_for_task(sys.argv[3:], exp, task), indent=1))
