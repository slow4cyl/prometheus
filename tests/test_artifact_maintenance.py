"""The archive resolver — where preserve (phase 1) and re-verify (phase 2)
finally meet. Preservation copies evidence to ARTIFACTS_ROOT/<task>/ but
verify_artifacts only resolves HERMES_HOME/workspace paths, so before the
resolver a GC'd workspace could never re-verify even with its files saved."""
import os

import artifact_maintenance as am


def _setup_archive(tmp_path, task="t_abc123", files=()):
    adir = tmp_path / "artifacts" / task
    adir.mkdir(parents=True)
    for name, content in files:
        (adir / name).write_text(content)
    return str(tmp_path / "artifacts")


def test_resolver_rewrites_missing_path_to_archive_copy(tmp_path, monkeypatch):
    root = _setup_archive(tmp_path, files=[("results.json", '{"ok": 1}')])
    monkeypatch.setattr(am, "ARTIFACTS_ROOT", root)
    out = am._resolve_archive(["/gone/workspace/results.json"], "t_abc123")
    assert out == [os.path.join(root, "t_abc123", "results.json")]


def test_resolver_matches_subdir_preservation_scheme(tmp_path, monkeypatch):
    # preserve_from_workspaces stores one-level-deep files as <subdir>__<name>
    root = _setup_archive(tmp_path, files=[("data__results.json", '{"ok": 1}')])
    monkeypatch.setattr(am, "ARTIFACTS_ROOT", root)
    out = am._resolve_archive(["data/results.json"], "t_abc123")
    assert out == [os.path.join(root, "t_abc123", "data__results.json")]


def test_resolver_leaves_existing_and_unarchived_paths_alone(tmp_path, monkeypatch):
    root = _setup_archive(tmp_path, files=[("a.json", "{}")])
    monkeypatch.setattr(am, "ARTIFACTS_ROOT", root)
    live = tmp_path / "live.json"
    live.write_text("{}")
    out = am._resolve_archive([str(live), "never/preserved.csv"], "t_abc123")
    assert out == [str(live), "never/preserved.csv"]   # existing kept, missing passes through


def test_resolver_is_inert_without_task_or_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "ARTIFACTS_ROOT", str(tmp_path / "nope"))
    assert am._resolve_archive(["x.json"], None) == ["x.json"]
    assert am._resolve_archive(["x.json"], "t_missing") == ["x.json"]
