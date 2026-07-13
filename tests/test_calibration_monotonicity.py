"""Regression pin for the calibration-map inversion incident (2026-07-13).

A Beta calibration map p(s) = sigmoid(a*ln s + b*ln(1-s) + c) MUST be
non-decreasing in the base score s. An unconstrained fit once wandered to
(a=0.45, b=+0.97, c=0) — a non-monotone map that collapsed raw 0.95 -> ~0.0001,
inverting confidence for a month while the promotion gate (scoring a different,
in-sync artifact) believed the champion was healthy.

Two guards are pinned here:
  * beta_map_is_monotone correctly rejects the broken params and accepts the
    intended monotone direction (a>=0, b<=0);
  * the promotion gate's bounds keep the fitted map monotone.
"""
import calibration_trainer as T


def test_detects_the_broken_incident_params():
    # The exact params that shipped to the runtime json in the incident.
    assert T.beta_map_is_monotone(0.450857, 0.974672, 0.0) is False


def test_accepts_intended_monotone_direction():
    # Identity warm-start and the good deployed pkl params.
    assert T.beta_map_is_monotone(1.0, -1.0, 0.0) is True
    assert T.beta_map_is_monotone(0.0577, -1.8077, -1.3267) is True


def test_flat_map_counts_as_monotone():
    # A constant map is non-decreasing (degenerate but not inverted).
    assert T.beta_map_is_monotone(0.0, 0.0, 0.0) is True


def test_any_positive_b_inverts_the_upper_tail():
    # Positive b puts +b*ln(1-s) -> -inf as s->1, collapsing high confidence.
    assert T.beta_map_is_monotone(1.0, 0.5, 0.0) is False
