#!/usr/bin/env python3
"""
Train the Beta calibration parameters for the Prometheus calibration model.

The multivariate model (lr_beta_1d) has AUC=0.891 but its Beta parameters
(beta_a=1.0, beta_b=-1.0, beta_c=0.0) were never fitted — they're at factory 
defaults, meaning calibrated_confidence is just a raw linear score, not a 
valid probability.

This script:
1. Loads the current model and all worker_results with known support/refute
2. Fits Beta calibration parameters using Platt scaling on a holdout split
3. Updates model_current.json with the fitted parameters
4. Reports calibration metrics (ECE, reliability diagram bins)

The holdout split (80/20) breaks the self-referential calibration loop:
the model is trained on data it hasn't already scored.

Run: python3 train_calibration.py [--dry-run]
"""

import json
import os
import sys
import time
import random
import sqlite3
from math import log, exp
from prometheus_paths import PROMETHEUS_DB as _PROMETHEUS_DB, under_home

try:
    import numpy as np
except ImportError:
    print("numpy required. Install with: uv pip install numpy", file=sys.stderr)
    sys.exit(1)

PROMETHEUS_DB = _PROMETHEUS_DB
MODEL_JSON = under_home("models", "calibration", "model_current.json")
BACKUP_DIR = under_home("models", "calibration", "backups")


def load_model():
    with open(MODEL_JSON) as f:
        return json.load(f)


def load_data():
    """Load worker_results with known support/refute and calibration features."""
    con = sqlite3.connect(f"file:{PROMETHEUS_DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    
    rows = con.execute("""
        SELECT id, experiment_id, calibrated_confidence, hypothesis_supported,
               experiment_type, mechanism_type, artifact_status,
               supported, domain
        FROM worker_results
        WHERE hypothesis_supported IS NOT NULL
          AND calibrated_confidence IS NOT NULL
    """).fetchall()
    
    con.close()
    
    data = []
    for r in rows:
        data.append({
            'wr_id': r['id'],
            'domain': r['domain'],
            'raw_conf': r['calibrated_confidence'],
            'supported': r['hypothesis_supported'],  # 1=supported, 0=refuted
            'exp_type': r['experiment_type'] or '',
            'mech_type': r['mechanism_type'] or '',
        })
    
    return data


def sigmoid(x):
    """Numerically stable sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + exp(-x))
    else:
        z = exp(x)
        return z / (1.0 + z)


def fit_platt(raw_scores, labels):
    """
    Fit Platt scaling: P(y=1) = sigmoid(A * s + B)
    where s = raw_score (0-1 scale).
    
    Uses Newton's method with regularization.
    Returns (A, B).
    """
    # Convert to logit space for better conditioning
    eps = 1e-12
    scores = np.clip(np.array(raw_scores, dtype=float), eps, 1.0 - eps)
    labels = np.array(labels, dtype=float)
    
    # Initialize with default Beta params
    A = 1.0
    B = 0.0
    
    # Newton's method with cross-validation-style regularization
    for iteration in range(100):
        # Predicted probabilities
        z = A * scores + B
        p = 1.0 / (1.0 + np.exp(-np.clip(z, -10, 10)))
        p = np.clip(p, eps, 1.0 - eps)
        
        # Gradient
        error = labels - p
        grad_A = np.sum(error * scores) - 0.01 * A  # L2 regularization
        grad_B = np.sum(error) - 0.01 * B
        
        # Hessian
        w = p * (1.0 - p)
        H_AA = -np.sum(w * scores * scores) - 0.01
        H_BB = -np.sum(w) - 0.01
        H_AB = -np.sum(w * scores)
        
        # Newton step
        det = H_AA * H_BB - H_AB * H_AB
        if abs(det) < 1e-12:
            break
        
        dA = -(H_BB * grad_A - H_AB * grad_B) / det
        dB = -(-H_AB * grad_A + H_AA * grad_B) / det
        
        step_size = min(1.0, 1.0 / (1.0 + abs(dA) + abs(dB)))
        A += step_size * dA
        B += step_size * dB
        
        if abs(dA) < 1e-6 and abs(dB) < 1e-6:
            break
    
    return A, B


def compute_ece(predictions, labels, n_bins=10):
    """Expected Calibration Error."""
    preds = np.array(predictions)
    labs = np.array(labels)
    
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    bin_details = []
    
    for i in range(n_bins):
        mask = (preds >= bin_edges[i]) & (preds < bin_edges[i + 1])
        if i == n_bins - 1:
            mask = (preds >= bin_edges[i]) & (preds <= bin_edges[i + 1])
        
        n = np.sum(mask)
        if n == 0:
            continue
        
        bin_conf = np.mean(preds[mask])
        bin_acc = np.mean(labs[mask])
        bin_ece = n * abs(bin_acc - bin_conf)
        ece += bin_ece
        bin_details.append({
            'bin': i,
            'range': f"{bin_edges[i]:.2f}-{bin_edges[i+1]:.2f}",
            'count': int(n),
            'confidence': float(bin_conf),
            'accuracy': float(bin_acc),
            'gap': float(bin_conf - bin_acc)
        })
    
    ece /= len(predictions)
    return ece, bin_details


def main():
    dry_run = '--dry-run' in sys.argv

    # DEPRECATED / GUARDED 2026-07-13. This script overwrites ONLY model_current.json
    # with its own unconstrained Beta fit, leaving model_current.pkl (which the
    # promotion gate in calibration_trainer.py scores) untouched. That desync served a
    # non-monotone, collapsed calibration map to the runtime (raw 0.95 -> ~0.0001) for a
    # month while the gate believed the champion was healthy and rejected every fix.
    # calibration_trainer.py now fits beta_a/b/c itself and writes both artifacts in
    # sync, so this script is redundant and actively harmful. It refuses to run unless
    # explicitly forced with --i-know-this-desyncs-the-model (never do this in the loop).
    if '--i-know-this-desyncs-the-model' not in sys.argv:
        print("train_calibration.py is DEPRECATED and refuses to run: it desyncs "
              "model_current.json from model_current.pkl. calibration_trainer.py fits "
              "beta params and writes both artifacts in sync. No action taken.")
        return 0

    print("Loading model...")
    model = load_model()
    print(f"  kind: {model.get('kind')}")
    print(f"  trained_at: {model.get('trained_at')}")
    print(f"  current beta_a={model.get('beta_a', 'N/A')} beta_b={model.get('beta_b', 'N/A')} beta_c={model.get('beta_c', 'N/A')}")
    
    print("\nLoading data...")
    data = load_data()
    print(f"  worker_results with known support/refute: {len(data)}")
    
    # Stratified 80/20 split
    supported = [d for d in data if d['supported'] == 1]
    refuted = [d for d in data if d['supported'] == 0]
    
    random.seed(42)
    random.shuffle(supported)
    random.shuffle(refuted)
    
    split_s = int(len(supported) * 0.8)
    split_r = int(len(refuted) * 0.8)
    
    train_data = supported[:split_s] + refuted[:split_r]
    test_data = supported[split_s:] + refuted[split_r:]
    
    random.shuffle(train_data)
    random.shuffle(test_data)
    
    print(f"  train: {len(train_data)} ({sum(1 for d in train_data if d['supported'])} supported)")
    print(f"  test:  {len(test_data)} ({sum(1 for d in test_data if d['supported'])} supported)")
    
    # Fit Platt scaling on training data
    train_scores = [d['raw_conf'] for d in train_data]
    train_labels = [d['supported'] for d in train_data]
    
    print("\nFitting Platt calibration...")
    A, B = fit_platt(train_scores, train_labels)
    print(f"  Fitted: beta_a={A:.4f} beta_b={B:.4f}")
    
    # Apply to test set
    test_scores = [d['raw_conf'] for d in test_data]
    test_labels = [d['supported'] for d in test_data]
    
    calibrated = []
    for s in test_scores:
        z = A * s + B
        p = sigmoid(z)
        calibrated.append(p)
    
    ece_before, bins_before = compute_ece(test_scores, test_labels)
    ece_after, bins_after = compute_ece(calibrated, test_labels)
    
    print(f"\n=== CALIBRATION RESULTS ===")
    print(f"  ECE before (default beta): {ece_before:.4f}")
    print(f"  ECE after  (fitted beta):  {ece_after:.4f}")
    print(f"  Improvement:               {(ece_before - ece_after) / max(ece_before, 1e-6) * 100:.1f}%")
    
    print(f"\n=== RELIABILITY DIAGRAM (after) ===")
    print(f"  {'Bin':<6} {'Range':<12} {'Count':>7} {'Conf':>7} {'Acc':>7} {'Gap':>8}")
    print(f"  {'-'*50}")
    for b in bins_after:
        print(f"  {b['bin']:<6} {b['range']:<12} {b['count']:>7} {b['confidence']:>6.3f} {b['accuracy']:>6.3f} {b['gap']:>+7.3f}")
    
    if dry_run:
        print(f"\nDRY RUN — not updating model_current.json")
        return
    
    # Backup current model
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f"model_current_{int(time.time())}.json")
    with open(backup_path, "w") as f:
        json.dump(model, f, indent=2)
    print(f"\nBacked up model to: {backup_path}")
    
    # Update model
    model['beta_a'] = round(A, 6)
    model['beta_b'] = round(B, 6)
    model['beta_c'] = 0.0
    model['trained_at'] = int(time.time())
    model['calibration_fit'] = {
        'method': 'platt_scaling_holdout',
        'train_size': len(train_data),
        'test_size': len(test_data),
        'ece_before': round(ece_before, 6),
        'ece_after': round(ece_after, 6),
        'fitted_at': int(time.time()),
    }
    
    with open(MODEL_JSON, "w") as f:
        json.dump(model, f, indent=2)
    
    print(f"Updated model_current.json with fitted Beta parameters")
    print(f"  beta_a={A:.4f} beta_b={B:.4f}")
    print(f"  ECE: {ece_before:.4f} → {ece_after:.4f}")


if __name__ == "__main__":
    main()