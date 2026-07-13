#!/usr/bin/env python3
"""calibration_features.py — single source of truth for the multivariate
confidence-calibration feature vector.

Imported by:
  - calibration_trainer.py   (fits the model)
  - write_worker_result.py   (scores new rows at write time)
  - backfill_calibration_mv.py (recomputes the whole table)

Keeping extraction here means the trainer, the runtime scorer, and the backfill
can never disagree about what a feature means. If you add/remove a feature,
bump FEATURE_VERSION; the trainer stamps it into the model artifact and the
runtime refuses to load a model whose FEATURE_VERSION doesn't match.
"""

FEATURE_VERSION = 2

# Order matters — the model is trained on exactly this column order.
FEATURE_NAMES = [
    "raw_confidence",
    "net_support",          # support_count - refute_count  (strongest signal)
    "support_count",
    "refute_count",
    "contradiction_count",
    "has_claim_evidence",   # 1 if any support/refute/contra recorded
    "is_universal_law",
    "is_empirical_correlation",
    "is_analogical",
    "is_empirical",
    "is_mechanistic",
    "artifact_verified",
    "artifact_failed",
    # --- Spatial features (exp_725308587541) ---
    # Moran's I spatial autocorrelation captures synchronized demand patterns
    # that predict grid stability better than individual consumption metrics.
    "moran_i_mean",         # Moran's I for mean consumption across locations
    "spatial_cv",           # Spatial coefficient of variation
    "spatial_lag_mean",     # Mean spatial lag (weighted neighbor average)
]


def extract_features(raw_confidence, experiment_type, mechanism_type,
                     artifact_status, support_count, refute_count,
                     contradiction_count,
                     moran_i_mean=None, spatial_cv=None, spatial_lag_mean=None):
    """Return a feature list in FEATURE_NAMES order from raw column values.

    All evidence counts default to 0 when the row has no joined knowledge_claim.
    Categorical strings are upper-cased and matched leniently.
    Spatial features default to 0.0 when not provided (backward compatible).
    """
    # Clamp to [0,1] as feature hygiene (2026-06-25): raw_confidence feeds the
    # model directly and is its strongest feature; a handful of rows carry
    # unnormalized values (e.g. 100.0, 5.0 from "5σ"/"100%" coercions). Only ~8
    # rows are affected today so this is NOT a fix for any current symptom — it's
    # a guard so the feature stays in-range if the worker population drifts.
    conf = float(raw_confidence) if raw_confidence is not None else 0.64
    conf = max(0.0, min(1.0, conf))
    sup = int(support_count or 0)
    ref = int(refute_count or 0)
    contra = int(contradiction_count or 0)
    etype = (experiment_type or "").strip().upper()
    mtype = (mechanism_type or "").strip().upper()
    art = (artifact_status or "").strip().upper()
    return [
        conf,
        sup - ref,
        sup,
        ref,
        contra,
        1 if (sup or ref or contra) else 0,
        1 if mtype == "UNIVERSAL_LAW" else 0,
        1 if mtype == "EMPIRICAL_CORRELATION" else 0,
        1 if etype == "ANALOGICAL" else 0,
        1 if etype in ("EMPIRICAL", "EMPIRICAL_VALIDATED") else 0,
        1 if etype == "MECHANISTIC" else 0,
        1 if art == "VERIFIED" else 0,
        1 if art == "FAILED" else 0,
        # Spatial features (exp_725308587541 — backward compatible)
        float(moran_i_mean) if moran_i_mean is not None else 0.0,
        float(spatial_cv) if spatial_cv is not None else 0.0,
        float(spatial_lag_mean) if spatial_lag_mean is not None else 0.0,
    ]
