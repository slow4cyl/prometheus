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
