#!/usr/bin/env python3
"""calibration_trainer.py — closed-loop, self-validating multivariate confidence
calibrator for Prometheus.

WHAT IT DOES (no human in the loop):
  1. Pulls every DECIDED worker_result (hypothesis_supported in {0,1}),
     deduped by row id, joined to its knowledge_claim for evidence counts.
  2. Builds features via calibration_features.extract_features (shared module).
  3. Trains an isotonic-calibrated logistic regression on a train split.
  4. Evaluates the CHALLENGER on a held-out test split: AUC, Brier, resolution.
  5. Compares to the deployed CHAMPION (re-scored on the SAME test split) AND to
     the scalar empirical map (the floor it must never drop below).
  6. PROMOTION GUARDRAIL — promotes the challenger ONLY if ALL hold:
        - challenger AUC >= champion AUC - EPS         (no discrimination regress)
        - challenger AUC >= scalar_map AUC + MIN_GAIN  (must beat the scalar floor)
        - challenger Brier <= champion Brier + EPS     (no calibration regress)
        - challenger trained on >= MIN_SAMPLES rows
     Otherwise it KEEPS the champion and logs why. A bad train run can never
     make the system worse.
  7. On promotion: writes model_current.pkl atomically + a versioned snapshot +
     appends a row to calibration_model_history (audit trail).

Safe to run on a schedule with zero supervision. Read-only on the data tables
(only reads worker_results/knowledge_claims); writes only to the model dir and
its own history table.

Exit codes: 0 = ran fine (promoted OR kept champion — both are success).
            1 = hard error (no data / sklearn missing / DB unreadable).
"""
import json
import os
import pickle
import sqlite3
import sys
import time
import tempfile
import traceback

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from calibration_features import (extract_features, FEATURE_NAMES,  # noqa: E402
                                  FEATURE_VERSION)
from prometheus_paths import KANBAN_DB, PROMETHEUS_DB, under_home  # noqa: E402

DB = PROMETHEUS_DB
MODEL_DIR = under_home("models", "calibration")
CURRENT = os.path.join(MODEL_DIR, "model_current.pkl")
NUMPY_CURRENT = os.path.join(MODEL_DIR, "model_current.json")  # runtime, sklearn-free

# Guardrail thresholds
EPS = 0.003           # tolerance: don't reject a challenger for trivial noise
MIN_GAIN = 0.02       # challenger must beat the scalar floor's AUC by this much
# Minimum AUC improvement over the EXISTING champion required to bother
# re-deploying. Prevents promote+backfill churn on every tiny data increment in
# the live system: we only redeploy when the model is MEANINGFULLY better, or to
# refresh a stale champion (see MAX_AGE_DAYS). First-ever model ignores this.
MIN_CHAMP_GAIN = 0.005
MAX_AGE_DAYS = 7      # force a refresh deploy if champion older than this
MIN_SAMPLES = 5000    # refuse to train on too little data
TEST_FRAC = 0.25
SEED = 42


def log(msg):
    print(f"[calib-trainer {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_data():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    cur = con.cursor()
    # Dedup by worker_results.id: the LEFT JOIN can fan out, so collapse to the
    # claim with the most evidence per row id.
    cur.execute("""
        SELECT wr.id, wr.confidence, wr.experiment_type, wr.mechanism_type,
               wr.artifact_status,
               MAX(COALESCE(kc.support_count,0))       AS sup,
               MAX(COALESCE(kc.refute_count,0))        AS ref,
               MAX(COALESCE(kc.contradiction_count,0)) AS contra,
               wr.hypothesis_supported AS y
        FROM worker_results wr
        LEFT JOIN knowledge_claims kc
               ON kc.first_experiment_id = wr.experiment_id
        WHERE wr.hypothesis_supported IN (0,1) AND wr.confidence IS NOT NULL
        GROUP BY wr.id
    """)
    rows = cur.fetchall()
    
    # --- EXTERNAL TRUTH INJECTION (closed validation loop) ---
    # Benchmark questions have known correct answers. We inject them into
    # the training data with y = actual correctness (not worker agreement).
    # This forces the model to learn truth-approximation, not just consensus.
    # Benchmark rows are weighted 50x to compensate for their small sample size
    # relative to the 200K+ worker-label rows.
    BENCHMARK_WEIGHT = 50
    try:
        import re as _re
        
        # Use a fresh connection for kanban lookups (the main conn cursor is exhausted)
        kconn = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=10)
        kconn.execute("PRAGMA busy_timeout=10000")
        
        # Get ALL worker_results with kanban_task_id (separate query, same DB)
        bcur = con.cursor()
        bench_rows = bcur.execute("""
            SELECT wr.id, wr.confidence, wr.experiment_type, wr.mechanism_type,
                   wr.artifact_status, wr.hypothesis_supported, wr.kanban_task_id
            FROM worker_results wr
            WHERE wr.hypothesis_supported IN (0,1) 
            AND wr.confidence IS NOT NULL
            AND wr.kanban_task_id IS NOT NULL
        """).fetchall()
        
        bench_added = 0
        for (_id, conf, et, mt, art, worker_says, task_id) in bench_rows:
            # Skip non-numeric confidence values
            try:
                float(conf)
            except (ValueError, TypeError):
                continue
            
            # Check if this task has a benchmark curiosity with known answer
            body_row = kconn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not body_row or not body_row[0]:
                continue
            m = _re.search(r'CURIOSITY_ID:\s*(\d+)', body_row[0])
            if not m:
                continue
            cid = int(m.group(1))
            
            # Get known answer from the main connection
            ccur = con.cursor()
            crow = ccur.execute(
                "SELECT known_answer FROM curiosities WHERE id = ? AND benchmark_id IS NOT NULL",
                (cid,)
            ).fetchone()
            if not crow:
                continue
            
            known = crow[0]
            actual_correct = (worker_says == 1) == (known == "true")
            y_external = 1 if actual_correct else 0
            
            # Add this row BENCHMARK_WEIGHT times to amplify the external signal
            sup = ref = contra = 0
            for _ in range(BENCHMARK_WEIGHT):
                rows.append((_id, conf, et, mt, art, sup, ref, contra, y_external))
            bench_added += 1
        
        kconn.close()
        if bench_added > 0:
            log(f"loaded {bench_added} benchmark rows (weighted {BENCHMARK_WEIGHT}x = {bench_added * BENCHMARK_WEIGHT} rows) with external truth labels")
    except Exception as e:
        log(f"benchmark injection skipped: {e}")
    
    con.close()
    X, y = [], []
    for (_id, conf, et, mt, art, sup, ref, contra, yy) in rows:
        try:
            X.append(extract_features(conf, et, mt, art, sup, ref, contra))
            y.append(int(yy))
        except (ValueError, TypeError):
            continue  # Skip rows with non-numeric confidence
    return X, y


def resolution(p, y, bins=20):
    base = sum(y) / len(y)
    n = len(y)
    res = 0.0
    edges = [i / bins for i in range(bins + 1)]
    for i in range(bins):
        lo, hi = edges[i], (edges[i + 1] if i < bins - 1 else 1.01)
        idx = [j for j in range(n) if lo <= min(max(p[j], 0), 1) < hi]
        if not idx:
            continue
        m = len(idx)
        mean_y = sum(y[j] for j in idx) / m
        res += m * (mean_y - base) ** 2
    return res / n


def main():
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import roc_auc_score, brier_score_loss
        from write_worker_result import calibrate_confidence as scalar_cal
    except Exception as e:
        log(f"HARD ERROR importing deps: {e}")
        return 1

    os.makedirs(MODEL_DIR, exist_ok=True)
    X, y = load_data()
    if len(y) < MIN_SAMPLES:
        log(f"HARD ERROR: only {len(y)} decided rows (< {MIN_SAMPLES}); aborting.")
        return 1
    X = np.array(X, float)
    y = np.array(y, int)
    log(f"loaded {len(y)} decided rows; base rate {y.mean():.4f}")

    Xtr, Xte, ytr, yte = train_test_split(
        X, y, test_size=TEST_FRAC, random_state=SEED, stratify=y)

    def _beta_a_b_c(z_tr, y_tr):
        """Fit Beta calibration: p = sigmoid(a*log(s) + b*log(1-s) + c)
        where s = sigmoid(z). Three-parameter smooth calibration that's
        more flexible than Platt (2 params) but avoids isotonic plateaus.
        Uses scipy L-BFGS-B to minimize log-loss."""
        try:
            from scipy.optimize import minimize
            eps = 1e-12
            s_tr = 1 / (1 + np.exp(-z_tr))
            # Clamp to avoid log(0)
            s_tr = np.clip(s_tr, eps, 1 - eps)

            def loss(params):
                a, b, c = params
                logit_beta = a * np.log(s_tr) + b * np.log(1 - s_tr) + c
                p = 1 / (1 + np.exp(-logit_beta))
                return -np.mean(y_tr * np.log(p + eps) + (1 - y_tr) * np.log(1 - p + eps))

            # Warm-start: a=1, b=-1, c=0  — identity-ish (sigmoid(z) -> logit)
            result = minimize(loss, [1.0, -1.0, 0.0], method='L-BFGS-B')
            return float(result.x[0]), float(result.x[1]), float(result.x[2])
        except ImportError:
            # scipy missing: fallback to Platt (2-param)
            return _platt_a_b(z_tr, y_tr) + (0.0,)

    def _platt_a_b(z_tr, y_tr):
        """Fit Platt (sigmoid) calibration: p = sigmoid(A*z + B).
        Returns (A, B) minimizing log-loss on (z_tr, y_tr). Used as fallback
        when scipy isn't available."""
        try:
            from scipy.optimize import minimize
            def loss(params):
                a, b = params
                p = 1 / (1 + np.exp(-(a * z_tr + b)))
                eps = 1e-12
                return -np.mean(y_tr * np.log(p + eps) + (1 - y_tr) * np.log(1 - p + eps))
            result = minimize(loss, [1.0, 0.0], method='L-BFGS-B')
            return float(result.x[0]), float(result.x[1])
        except ImportError:
            zz = np.sort(z_tr)
            best = (1.0, 0.0, float('inf'))
            for a_guess in [0.3, 0.5, 0.7, 1.0, 1.3, 1.5, 2.0, 3.0]:
                for b_guess in [-0.5, 0.0, 0.5]:
                    p = 1 / (1 + np.exp(-(a_guess * zz + b_guess)))
                    eps = 1e-12
                    loss = -np.mean(y_tr * np.log(p + eps) + (1 - y_tr) * np.log(1 - p + eps))
                    if loss < best[2]:
                        best = (a_guess, b_guess, loss)
            return best[0], best[1]

    def fit_1d_pipeline(Xf, yf):
        """Fit scaler -> LR linear score z -> Beta calibration (smooth,
        continuous, 3-param). Avoids isotonic's frozen-value plateaus
        while providing enough flexibility for good calibration."""
        sc = StandardScaler().fit(Xf)
        lr = LogisticRegression(max_iter=4000, C=1.0).fit(sc.transform(Xf), yf)
        z_tr = sc.transform(Xf) @ lr.coef_[0] + lr.intercept_[0]
        beta_a, beta_b, beta_c = _beta_a_b_c(z_tr, yf)
        return {
            "feature_version": FEATURE_VERSION, "feature_names": FEATURE_NAMES,
            "kind": "lr_beta_1d",
            "w": lr.coef_[0].astype(float).tolist(), "b": float(lr.intercept_[0]),
            "mean": sc.mean_.astype(float).tolist(),
            "std": sc.scale_.astype(float).tolist(),
            "beta_a": beta_a, "beta_b": beta_b, "beta_c": beta_c,
        }

    def score_1d(art, Xf):
        """Pure-numpy scoring of a 1d pipeline artifact (matches runtime exactly).
        Handles lr_beta_1d (smooth Beta calibration), lr_platt_1d (sigmoid),
        and legacy lr_isotonic_1d (LUT)."""
        w = np.array(art["w"]); mean = np.array(art["mean"]); std = np.array(art["std"])
        z = (np.array(Xf, float) - mean) / std @ w + art["b"]
        kind = art.get("kind", "lr_isotonic_1d")
        if kind == "lr_beta_1d":
            # Beta calibration: sigmoid(a*log(s) + b*log(1-s) + c)
            eps = 1e-12
            s = 1.0 / (1.0 + np.exp(-z))
            s = np.clip(s, eps, 1.0 - eps)
            logit_beta = art["beta_a"] * np.log(s) + art["beta_b"] * np.log(1.0 - s) + art["beta_c"]
            p = 1.0 / (1.0 + np.exp(-logit_beta))
            return np.clip(p, 0.0, 1.0)
        if kind == "lr_platt_1d":
            # Smooth sigmoid calibration — continuous, no plateaus
            p = 1 / (1 + np.exp(-(art["platt_a"] * z + art["platt_b"])))
            return np.clip(p, 0.0, 1.0)
        # Legacy: lr_isotonic_1d — LUT interpolation
        return np.interp(z, np.array(art["lut_z"]), np.array(art["lut_p"]))

    # ---- CHALLENGER: the exact pipeline we would deploy, trained on the train split
    chal = fit_1d_pipeline(Xtr, ytr)
    p_ch = score_1d(chal, Xte)
    auc_ch = roc_auc_score(yte, p_ch)
    brier_ch = brier_score_loss(yte, np.clip(p_ch, 0, 1))
    res_ch = resolution(p_ch, list(yte))

    # ---- Scalar floor on the SAME test split
    p_sc = np.array([scalar_cal(c) for c in Xte[:, 0]])
    auc_sc = roc_auc_score(yte, p_sc)
    brier_sc = brier_score_loss(yte, np.clip(p_sc, 0, 1))

    # ---- Champion (if any) re-scored on the SAME test split
    champ = None
    auc_champ = brier_champ = None
    if os.path.exists(CURRENT):
        try:
            with open(CURRENT, "rb") as f:
                champ = pickle.load(f)
            if champ.get("feature_version") == FEATURE_VERSION and champ.get("kind", "") in ("lr_beta_1d", "lr_platt_1d", "lr_isotonic_1d"):
                p_cm = score_1d(champ, Xte)
                auc_champ = roc_auc_score(yte, p_cm)
                brier_champ = brier_score_loss(yte, np.clip(p_cm, 0, 1))
            else:
                log("champion stale/incompatible; treating as none.")
                champ = None
        except Exception as e:
            log(f"could not score champion ({e}); treating as none.")
            champ = None

    log(f"CHALLENGER  AUC={auc_ch:.4f} Brier={brier_ch:.4f} Res={res_ch:.4f}")
    log(f"scalar floor AUC={auc_sc:.4f} Brier={brier_sc:.4f}")
    if champ:
        log(f"CHAMPION    AUC={auc_champ:.4f} Brier={brier_champ:.4f}")

    # ---- PROMOTION GUARDRAIL ----
    # Hard floors (always enforced): never deploy something worse than the scalar
    # map, and never regress vs the champion on AUC or Brier.
    reasons = []
    if auc_ch < auc_sc + MIN_GAIN:
        reasons.append(f"AUC {auc_ch:.4f} < scalar floor {auc_sc:.4f}+{MIN_GAIN}")
    if champ is not None:
        if auc_ch < auc_champ - EPS:
            reasons.append(f"AUC regressed vs champion ({auc_ch:.4f} < {auc_champ:.4f})")
        if brier_ch > brier_champ + EPS:
            reasons.append(f"Brier regressed vs champion ({brier_ch:.4f} > {brier_champ:.4f})")
        # Churn guard: with a healthy champion, only redeploy if MEANINGFULLY
        # better OR the champion is stale (force a periodic refresh on new data).
        if not reasons:
            champ_age_days = (time.time() - champ.get("trained_at", 0)) / 86400.0
            meaningful = auc_ch >= auc_champ + MIN_CHAMP_GAIN
            stale = champ_age_days >= MAX_AGE_DAYS
            if not (meaningful or stale):
                reasons.append(
                    f"no meaningful gain (dAUC={auc_ch-auc_champ:+.4f} < "
                    f"{MIN_CHAMP_GAIN}) and champion fresh ({champ_age_days:.1f}d)")

    promote = len(reasons) == 0
    ts = int(time.time())
    record_history(ts, len(y), auc_ch, brier_ch, res_ch, auc_sc, brier_sc,
                   auc_champ, brier_champ, promote, "; ".join(reasons))

    if not promote:
        log(f"KEEPING CHAMPION — challenger rejected: {'; '.join(reasons)}")
        return 0

    # ---- DEPLOY: refit the SAME pipeline on ALL data, write atomically.
    artifact = fit_1d_pipeline(X, y)
    artifact.update({
        "trained_at": ts, "n_train": len(y), "base_rate": float(y.mean()),
        "metrics": {"auc": auc_ch, "brier": brier_ch, "resolution": res_ch,
                    "scalar_auc": auc_sc, "scalar_brier": brier_sc},
    })
    # pickle (next-run champion eval) + json (sklearn-free runtime) — both atomic
    fd, tmp = tempfile.mkstemp(dir=MODEL_DIR, suffix=".pkl")
    with os.fdopen(fd, "wb") as f:
        pickle.dump(artifact, f)
    os.replace(tmp, CURRENT)
    with open(os.path.join(MODEL_DIR, f"model_{ts}.pkl"), "wb") as f:
        pickle.dump(artifact, f)
    fd2, tmp2 = tempfile.mkstemp(dir=MODEL_DIR, suffix=".json")
    with os.fdopen(fd2, "w") as f:
        json.dump(artifact, f)
    os.replace(tmp2, NUMPY_CURRENT)
    log(f"PROMOTED -> {CURRENT} + {NUMPY_CURRENT}  "
        f"(AUC {auc_ch:.4f}, Brier {brier_ch:.4f}, Res {res_ch:.4f})")
    return 0


def record_history(ts, n, auc_ch, brier_ch, res_ch, auc_sc, brier_sc,
                   auc_champ, brier_champ, promoted, reasons):
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""CREATE TABLE IF NOT EXISTS calibration_model_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trained_at INTEGER, n_train INTEGER,
        challenger_auc REAL, challenger_brier REAL, challenger_resolution REAL,
        scalar_auc REAL, scalar_brier REAL,
        champion_auc REAL, champion_brier REAL,
        promoted INTEGER, reasons TEXT)""")
    con.execute("""INSERT INTO calibration_model_history
        (trained_at,n_train,challenger_auc,challenger_brier,challenger_resolution,
         scalar_auc,scalar_brier,champion_auc,champion_brier,promoted,reasons)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (ts, n, auc_ch, brier_ch, res_ch, auc_sc, brier_sc,
         auc_champ, brier_champ, 1 if promoted else 0, reasons))
    con.commit()
    con.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
