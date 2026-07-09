"""Schema is single-source and the paths module honors HERMES_HOME."""
import os
import sqlite3

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
SCHEMA = os.path.join(REPO_ROOT, "schema", "prometheus.schema.sql")

CORE_TABLES = {
    "experiments", "worker_results", "knowledge_claims", "claim_evidence",
    "claim_scopes", "discovery_candidates", "world_groundings", "curiosities",
}


def test_schema_file_bootstraps_a_complete_database(tmp_path):
    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.executescript(open(SCHEMA).read())
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert len(tables) >= 60, f"only {len(tables)} tables — schema truncated?"
    missing = CORE_TABLES - tables
    assert not missing, f"core tables missing from schema: {missing}"


def test_init_db_uses_canonical_schema_not_a_stale_subset():
    """The old embedded 17-table bootstrap must be gone for good."""
    import inspect
    import prometheus_db
    src = inspect.getsource(prometheus_db.init_db)
    assert "prometheus.schema.sql" in src
    # the stale inline script defined tables directly; the rewrite must not
    assert "CREATE TABLE" not in src


def test_init_db_creates_the_full_schema(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import prometheus_paths, prometheus_db
    importlib.reload(prometheus_paths)
    importlib.reload(prometheus_db)
    prometheus_db.init_db()
    conn = sqlite3.connect(tmp_path / "prometheus.db")
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    conn.close()
    assert n >= 60


def test_paths_module_honors_hermes_home(tmp_path, monkeypatch):
    import importlib
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import prometheus_paths
    importlib.reload(prometheus_paths)
    assert prometheus_paths.HERMES_HOME == str(tmp_path)
    assert prometheus_paths.PROMETHEUS_DB == os.path.join(str(tmp_path), "prometheus.db")
    assert prometheus_paths.under_home("x", "y").startswith(str(tmp_path))
