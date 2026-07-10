"""Referent-awareness pins for central_quantity_drift.

The detector may only compare two numbers when they share a REFERENT — same
role, not just same topic. Cards #60779 and #66039 (the top two shelf cards)
false-flagged from three referent confusions, pinned here alongside the
existing #58935 refuted-clause behavior and every documented true positive.
"""
import discovery_routing as routing


def drift(h, r, s=""):
    return routing.central_quantity_drift(h, r, s)


# --- the two top-shelf false positives (must never fire) -------------------

def test_word_endings_never_bind_single_letter_labels():
    # "gap"/"overlap" must not read as p=… — the root of both card flags.
    q = routing._labeled_quantities("universal gap=25.7pp while overlap=0.31")
    assert "p" not in q


def test_p_value_vs_pp_gap_does_not_fire_60779():
    assert drift(
        "universal features win (mean AUC=0.838, p=0.049)",
        "calibrated_confidence alone achieves AUC=0.841",
        "boundary mapping: universal gap=25.7pp in the narrowed regime") is None


def test_point_estimate_vs_threshold_does_not_fire_66039():
    assert drift(
        "RF advantage reverses at 45 dims (IF wins, gap=-0.025)",
        "the specific quantitative gaps hold for contextual anomalies",
        "SURVIVING CORE: RF wins (gap>0.05) in 62% of conditions") is None


def test_threshold_operator_binding_is_not_a_statistic():
    # Criteria/bounds are excluded outright, in any text.
    assert routing._labeled_quantities("holds only for d<2.5 and requires p<0.05") == {}


def test_scope_metrics_are_not_compared_as_labeled_stats():
    # A narrowed scope's own metrics differ from the headline by construction
    # (#60779: a different model's AUC under the same label).
    assert drift("classifier reaches AUC=0.838", "",
                 "narrowed regime boundary map: overall AUC=0.6125") is None


def test_unit_mismatch_is_not_compared():
    assert drift("effect d=25.0pp on retention", "effect d=0.31 (standardized)") is None


# --- documented true positives (must keep firing) ---------------------------

def test_true_self_disagreement_still_fires():
    got = drift("correlation r=-0.819 across runs", "the r=-0.707 relationship")
    assert got is not None and got[2] > 0.05


def test_range_vs_point_still_fires_65186():
    assert drift("scaling regime p=2-3 across sizes", "measured p=0.25") is not None


def test_direction_conflict_still_fires_68481():
    got = drift("the formula underpredicts by 3.07x", "the formula overpredicts by 173%")
    assert got == ("underpredicts", "overpredicts", 1.0)


# --- pre-existing guards (unchanged behavior) --------------------------------

def test_refuted_clause_is_stripped_58935():
    assert drift(
        "Side A's prediction that clipping destroys the norm advantage "
        "(AUC~0.50) is REFUTED; the effect holds at AUC=0.76",
        "the AUC=0.74 norm advantage") is None


def test_lone_integers_skipped_62239():
    assert drift("stable out to t=20000 steps", "diverges after t=8000 steps") is None
