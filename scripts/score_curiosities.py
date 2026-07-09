#!/usr/bin/env python3
"""
score_curiosities.py — Pre-compute multi-objective scores for active curiosities.

Uses curiosity_multi_scorer.py (P(confirm), P(novel), P(expand), combined_score).
Only scores items that are unscored or stale (>30 min old).
Flock prevents overlapping runs. Stays SILENT when nothing to do.
"""
import os
from prometheus_paths import HERMES_HOME as _PP_HERMES_HOME
import sys
import time
import fcntl
import sqlite3
from db_retry import get_db

HERMES = _PP_HERMES_HOME
SCRIPTS = os.path.join(HERMES, "scripts")
DB_PATH = os.path.join(HERMES, "prometheus.db")
LOCK = os.path.join(HERMES, ".score_curiosities.lock")
STAMP = os.path.join(HERMES, ".score_curiosities_last")
sys.path.insert(0, SCRIPTS)

STALE_SECONDS = 30 * 60  # 30 minutes
OVERRIDE_STATUS = os.path.join(HERMES, "prior_override_status.json")
SURPRISE_MIN_N = 10          # ignore domains with too few preregistrations to trust
META_CAL = os.path.join(HERMES, "meta_transfer_calibration.json")
META_CAL_MIN_N = 20          # ignore the snapshot until enough probes are scored
MECH_CAL = os.path.join(HERMES, "mechanism_calibration.json")
SHAPE_MIN_N = 200            # per-shape sample floor before its rate is trusted
SHAPE_MIN_GAP_PP = 3.0       # ignore gaps inside noise; discount only real over-trust


def load_surprise_weights():
    """Per-domain 'surprise' in [0,1] from prior_override_report's by-domain
    confirmation rate: how far BELOW the global confirmation baseline a domain's
    preregistered priors land (0 when at/above baseline or under SURPRISE_MIN_N
    samples). A high value marks a domain where the fleet is often wrong — where
    an experiment buys the most information. Best-effort: any problem → {} (the
    scorer then behaves exactly as before)."""
    try:
        import json
        with open(OVERRIDE_STATUS) as f:
            d = json.load(f)
        baseline = (d.get("prior_confirmation_rate_pct") or 72.0) / 100.0
        if baseline <= 0:
            return {}
        out = {}
        for dom, st in (d.get("by_domain") or {}).items():
            n = st.get("n") or 0
            if n < SURPRISE_MIN_N:
                continue
            conf = (st.get("confirmation_pct") or 100.0) / 100.0
            surprise = (baseline - conf) / baseline      # >0 only when below baseline
            if surprise > 0:
                out[dom] = round(min(surprise, 1.0), 4)
        return out
    except Exception:
        return {}


def load_transfer_discount():
    """Bounded p_confirm multiplier for [TRANSFER] questions from the meta-claim
    prober's measured self-transfer record (meta_claim_prober.py --reconcile
    writes the snapshot hourly). The snapshot's overconfidence_rate is the
    fraction of preregistered "this generalizes" beliefs that did NOT hold in
    an unseen domain; the discount is 1 - min(0.25, rate/2) — bounded so it
    re-ranks the confirm lane rather than starving transfer questions.
    Best-effort: missing/small-n snapshot -> 1.0 (scorer behaves as before)."""
    try:
        import json
        with open(META_CAL) as f:
            d = json.load(f)
        if (d.get("n_scored") or 0) < META_CAL_MIN_N:
            return 1.0
        over = d.get("overconfidence_rate") or 0.0
        return round(1.0 - min(0.25, over / 2.0), 4)
    except Exception:
        return 1.0


def load_shape_discounts():
    """Per-mechanism-shape p_confirm multiplier from mechanism_calibration.json
    (6h cron; framing-independent sign-vs-sign scoring). A shape whose
    preregistered priors confirm well BELOW the overall rate is one the fleet
    over-trusts (MONOTONIC −11.5pp is the worst), so questions of that shape
    get a bounded confirm-lane discount: 1 − min(0.15, gap_pp/200), i.e.
    MONOTONIC ⇒ ×0.9425. Shapes above baseline (TRANSFER +2.2) get nothing —
    a discount only, never a boost. Best-effort: missing file / small n /
    small gap → {} (scorer behaves as before)."""
    try:
        import json
        with open(MECH_CAL) as f:
            d = json.load(f)
        out = {}
        for cls, st in (d.get("by_mechanism") or {}).items():
            if (st.get("n") or 0) < SHAPE_MIN_N:
                continue
            gap = -(st.get("vs_overall_pp") or 0.0)   # >0 when below overall
            if gap < SHAPE_MIN_GAP_PP:
                continue
            out[cls] = round(1.0 - min(0.15, gap / 200.0), 4)
        return out
    except Exception:
        return {}


def score_all_curiosities():
    """Score unscored/stale curiosities using multi-objective scorer."""
    from curiosity_multi_scorer import score_curiosity, load_model
    from mechanism_calibration import classify_mechanism

    model = load_model()
    if not model:
        print("ERROR: No trained model found. Run curiosity_multi_scorer.py --calibrate first.")
        return

    conn = get_db()
    now = time.time()
    surprise_weights = load_surprise_weights()
    transfer_discount = load_transfer_discount()
    shape_discounts = load_shape_discounts()

    # Only score items without combined_score or stale (>30 min old). LEFT JOIN
    # resolves each curiosity's domain (source_experiment→experiments.domain
    # covers ~93%, source_result_id→worker_results.domain backs up the rest) so
    # the scorer can apply the per-domain surprise weight; NULL domain → no bonus.
    cur_rows = conn.execute("""
        SELECT c.id, c.text, COALESCE(c.evidence_depth, 0) as evidence_depth,
               COALESCE(e.domain, wr.domain) AS domain
        FROM curiosities c
        LEFT JOIN experiments e ON e.id = c.source_experiment
        LEFT JOIN worker_results wr ON wr.id = c.source_result_id
        WHERE c.status='active'
        AND (c.combined_score IS NULL OR c.score_updated_at IS NULL OR c.score_updated_at < ?)
        ORDER BY c.created_at DESC
        LIMIT 2000
    """, (now - STALE_SECONDS,)).fetchall()

    if not cur_rows:
        print("All curiosities scored and fresh. Nothing to do.")
        conn.close()
        return

    print(f"Scoring {len(cur_rows)} unscored/stale curiosities "
          f"({len(surprise_weights)} surprising domains weighted, "
          f"transfer p_confirm x{transfer_discount}, "
          f"shape discounts {shape_discounts or 'none'})...")

    updated = 0
    errors = 0
    boosted = 0

    for r in cur_rows:
        cur_id = r["id"]
        text = r["text"] or ""
        if len(text) < 10:
            continue

        surprise = surprise_weights.get(r["domain"], 0.0) if r["domain"] else 0.0
        shape_discount = (shape_discounts.get(classify_mechanism(text), 1.0)
                          if shape_discounts else 1.0)
        try:
            s = score_curiosity(text, model, evidence_depth=r["evidence_depth"],
                                surprise=surprise,
                                transfer_confirm_discount=transfer_discount,
                                shape_confirm_discount=shape_discount)
            if s.get("surprise_bonus"):
                boosted += 1
            conn.execute("""
                UPDATE curiosities
                SET p_confirm=?, p_novel=?, p_expand=?, p_break=?, combined_score=?,
                    score_updated_at=?
                WHERE id=?
            """, (s['p_confirm'], s['p_novel'], s['p_expand'], s.get('p_break'), s['combined'], now, cur_id))
            updated += 1
        except Exception:
            errors += 1

    conn.commit()

    total_active = conn.execute(
        "SELECT COUNT(*) FROM curiosities WHERE status='active'"
    ).fetchone()[0]
    scored_count = conn.execute(
        "SELECT COUNT(*) FROM curiosities WHERE status='active' AND combined_score IS NOT NULL"
    ).fetchone()[0]

    conn.close()
    print(f"Scored: {updated} updated, {errors} errors, {boosted} surprise-boosted")
    print(f"DB: {total_active} active, {scored_count} scored")


if __name__ == "__main__":
    lf = open(LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # another run in progress; stay silent

    try:
        score_all_curiosities()
        with open(STAMP, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()
