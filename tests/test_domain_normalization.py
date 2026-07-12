"""Domain normalization invariants.

The 2026-06-08 incident: greedy mappings swept unrelated experiments into
mega-buckets and were removed from write_worker_result as the root cause of
domain mislabeling — but a stale copy in apply_worker_results kept applying
them for a month. These tests pin both the policy and the single-source
delegation so the drift cannot silently return.
"""
import write_worker_result as wwr
import apply_worker_results as awr

# The mappings banned on 2026-06-08. Identity-preservation is the invariant.
BANNED_LOSSY = [
    "general", "rlhf", "rag", "rag_dedup", "rag_safety", "defense", "attack",
    "synthesis", "meta", "meta_cognition", "cross_lingual", "distillation",
    "adversarial_detection", "ml_security", "tardigrade_biology", "ensemble",
    "dispatch", "dict",
]

# True synonyms that must keep collapsing.
KEPT_SYNONYMS = {
    "prompt_injection": "injection_detection",
    "prompt_injection_detection": "injection_detection",
    "hallucination_detection": "injection_detection",
    "cross_pollination": "cross_domain",
    "meta_research": "meta_analysis",
    "meta_learning": "meta_analysis",
    "embeddings": "embedding",
    "ai_safety": "safety",
    "ml_safety": "safety",
    "financial_markets": "finance",
}


def test_banned_lossy_mappings_stay_identity():
    for d in BANNED_LOSSY:
        assert wwr.normalize_domain(d) == d, f"lossy mapping re-added for {d!r}"


def test_kept_synonyms_still_collapse():
    for src, dst in KEPT_SYNONYMS.items():
        assert wwr.normalize_domain(src) == dst


def test_apply_stage_delegates_to_write_stage():
    """The regression that motivated this file: two copies drifting."""
    probes = BANNED_LOSSY + list(KEPT_SYNONYMS) + ["Some-Novel Domain/x", ""]
    for d in probes:
        assert awr.normalize_domain(d) == wwr.normalize_domain(d), d


def test_normalization_is_idempotent_and_canonicalizes_format():
    assert wwr.normalize_domain("Prompt Injection") == "injection_detection"
    assert wwr.normalize_domain("cross-pollination") == "cross_domain"
    out = wwr.normalize_domain("Weird--Domain  Name")
    assert out == wwr.normalize_domain(out)


def test_empty_and_none_pass_through():
    assert wwr.normalize_domain("") == ""
    assert wwr.normalize_domain(None) is None


def test_all_policy_copies_agree_with_canonical():
    """2026-07-09: TWO more stale copies found (topology_common._MERGES and
    normalize_all_domains.SEMANTIC_MERGES — the latter bulk-REWRITES both DBs
    every 15 minutes via domain-taxonomy-maintenance). Pin every module that
    exposes domain canonicalization to the single source of truth."""
    import topology_common
    import normalize_all_domains
    probes = BANNED_LOSSY + list(KEPT_SYNONYMS) + ["Some-Novel Domain/x"]
    for d in probes:
        assert topology_common.normalize_domain(d) == wwr.normalize_domain(d), d
        assert normalize_all_domains.canonical_for(d) == (wwr.normalize_domain(d) or ""), d


def test_empty_domains_are_not_swept_into_calibration():
    """The NULL->'calibration' bulk UPDATE was the 'general' sweep in DB form;
    normalize_all_domains must no longer contain it."""
    import inspect, normalize_all_domains
    src = inspect.getsource(normalize_all_domains)
    assert "SET domain = 'calibration' WHERE" not in src


# --- Step-5 rebuild id stability (2026-07-12) --------------------------------
# The old DELETE+re-INSERT rebuild under AUTOINCREMENT handed every domain a
# fresh id each run and orphaned every integer-FK child (the Domains-view
# 0/0 incident). The rebuild is now a name-keyed upsert: ids must be stable
# across runs, and names that vanish from experiments must still be deleted.

def test_domains_rebuild_preserves_ids_and_deletes_stale(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    db = tmp_path / "p.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE experiments (id TEXT PRIMARY KEY, domain TEXT);
        CREATE TABLE worker_results (id INTEGER PRIMARY KEY, domain TEXT);
        CREATE TABLE domains (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL, confidence REAL DEFAULT 0.5,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO experiments VALUES ('e1','physics'),('e2','physics'),('e3','biology');
    """)
    conn.commit()
    conn.close()

    script = os.path.join(os.path.dirname(__file__), "..", "scripts",
                          "normalize_all_domains.py")

    def run():
        r = subprocess.run([sys.executable, script, str(db)],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        c = sqlite3.connect(db)
        ids = dict(c.execute("SELECT name, id FROM domains"))
        c.close()
        return ids

    first = run()
    second = run()
    assert second == first, "re-running the rebuild must not change domain ids"

    # a new domain gets a new id; existing ids stay put
    c = sqlite3.connect(db)
    c.execute("INSERT INTO experiments VALUES ('e4','chemistry')")
    c.commit()
    c.close()
    third = run()
    assert third["physics"] == first["physics"]
    assert third["biology"] == first["biology"]
    assert "chemistry" in third

    # a domain that vanishes from experiments is deleted, others keep their ids
    c = sqlite3.connect(db)
    c.execute("DELETE FROM experiments WHERE domain='biology'")
    c.commit()
    c.close()
    fourth = run()
    assert "biology" not in fourth
    assert fourth["physics"] == first["physics"]
