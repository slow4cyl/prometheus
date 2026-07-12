"""Invariants for the maturity policy and confidence arithmetic."""
import maturity
from write_worker_result import clamp_confidence, calibrate_confidence


def _base(**over):
    kw = dict(wsc=0.0, refute_count=0, contradiction_count=0, n_retests=0,
              n_formal_replications=0, spurious_agreement=0.0)
    kw.update(over)
    return maturity.compute_maturity(**kw)


def test_disputed_takes_priority_over_everything():
    t = maturity.MATURITY_THRESHOLDS["disputed_contradiction_min"]
    res = _base(wsc=99.0, n_retests=50, n_formal_replications=10,
                contradiction_count=t)
    assert res.status == "DISPUTED"


def test_below_disputed_threshold_is_not_disputed():
    t = maturity.MATURITY_THRESHOLDS["disputed_contradiction_min"]
    res = _base(contradiction_count=t - 1)
    assert res.status != "DISPUTED"


def test_zero_signal_claim_reaches_no_tier():
    res = _base()
    assert res.status in (None, "", "NONE") or res.status not in ("ESTABLISHED",)


def test_result_is_always_explainable():
    for res in (_base(), _base(wsc=10, n_retests=3)):
        assert isinstance(res.passed_checks, list)
        assert isinstance(res.failed_checks, list)
        assert res.passed_checks or res.failed_checks


def test_none_inputs_are_normalized_not_crashing():
    res = maturity.compute_maturity(wsc=None, refute_count=None,
                                    contradiction_count=None, n_retests=None,
                                    n_formal_replications=None,
                                    spurious_agreement=None)
    assert res is not None


# --- confidence -------------------------------------------------------------

def test_clamp_confidence_ranges():
    assert clamp_confidence(None) == 0.85          # documented default
    assert clamp_confidence("garbage") == 0.85
    assert clamp_confidence(-3) == 0.0
    assert clamp_confidence(0.5) == 0.5
    assert clamp_confidence(1.0) == 1.0
    assert clamp_confidence(55) == 0.55            # percent-style input
    assert clamp_confidence(100.0) == 1.0
    assert 0.0 <= clamp_confidence(1e9) <= 1.0


def test_calibrate_confidence_pins_the_audit_map():
    # Bin values derived from the 15,583-finding audit; exact map is policy.
    assert calibrate_confidence(0.50) == 0.7857
    assert calibrate_confidence(0.00) == 0.5455
    assert calibrate_confidence(1.00) == 0.9167


def test_calibrate_confidence_interpolates_and_clamps_edges():
    mid = calibrate_confidence(0.525)  # rounds to a half-bin between 0.50/0.55
    assert 0.7857 - 1e-9 <= mid <= 0.8750 + 1e-9
    assert calibrate_confidence(-5.0) == calibrate_confidence(-0.30)
    assert calibrate_confidence(5.0) == calibrate_confidence(1.00)


def test_calibration_never_reports_certainty():
    for raw in (0.0, 0.35, 0.6, 0.85, 1.0):
        assert 0.0 < calibrate_confidence(raw) < 1.0


# --- world gate (2026-07-09): gate, not weight; arm-file controlled ---------

def _establishable(**over):
    kw = dict(wsc=9.0, refute_count=0, contradiction_count=0, n_retests=3,
              n_formal_replications=1, spurious_agreement=0.0, n_break_survivals=1,
              n_blind_supports=1, n_stamped_supports=2, independence_armed=True)
    kw.update(over)
    return maturity.compute_maturity(**kw)


def test_world_gate_blocks_established_when_armed_and_world_refuted():
    r = _establishable(world_refuted=1, world_gate_armed=True)
    assert r.status == "REPLICATED"
    assert "world" in (r.blocking_reason or "").lower()


def test_world_gate_inert_when_disarmed():
    # Disarmed path must be byte-identical to pre-gate: a world FAILS does not block.
    assert _establishable(world_refuted=1, world_gate_armed=False).status == "ESTABLISHED"


def test_world_gate_does_not_block_when_world_holds():
    assert _establishable(world_refuted=0, world_gate_armed=True).status == "ESTABLISHED"


def test_world_gate_never_demotes_below_replicated():
    # A CANDIDATE-level claim with a world FAILS stays CANDIDATE — the gate only
    # caps the REPLICATED->ESTABLISHED promotion, never demotes further.
    r = maturity.compute_maturity(wsc=0.6, refute_count=0, contradiction_count=0,
                                  n_retests=0, n_formal_replications=0, spurious_agreement=0.0,
                                  world_refuted=1, world_gate_armed=True)
    assert r.status not in ("ESTABLISHED", "REPLICATED")


# --- claim-reconciliation gate (2026-07-12): scope_conflict caps at CANDIDATE

def test_scope_conflict_caps_at_candidate():
    r = _establishable(scope_conflict=1)
    assert r.status == "CANDIDATE"
    assert "scope_conflict" in " ".join(r.failed_checks)
    assert "unreconciled" in (r.blocking_reason or "").lower()


def test_scope_conflict_null_and_cleared_are_inert():
    # NULL (never reviewed) and 0 (reconciled) must be byte-identical to pre-gate.
    assert _establishable(scope_conflict=None).status == "ESTABLISHED"
    assert _establishable(scope_conflict=0).status == "ESTABLISHED"


def test_scope_conflict_does_not_fire_below_candidate_band():
    # A claim without CANDIDATE-level support has nothing to cap — falls through
    # to the normal wsc check (mirrors circular_construction / method_code_mismatch).
    r = maturity.compute_maturity(wsc=0.0, refute_count=0, contradiction_count=0,
                                  n_retests=0, n_formal_replications=0,
                                  spurious_agreement=0.0, scope_conflict=1)
    assert r.status is None
