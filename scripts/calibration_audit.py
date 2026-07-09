#!/usr/bin/env python3
"""
calibration_audit.py — Measure calibration model quality against external ground truth.

The calibration model (calibrated_confidence) is trained on worker labels 
(hypothesis_supported). This script checks it against KNOWN answers from 
benchmark questions — external ground truth the model has never seen.

Runs every 30m. Reports:
- Brier score (lower = better, 0 = perfect)
- Reliability (how well confidence matches actual correctness rate)
- Systematic bias (overconfident vs underconfident)
- Per-depth breakdown

If calibration is systematically wrong on benchmark data, this is the signal
that the closed validation loop needs to feed back into the calibration trainer.
"""

import json
import os
import re
import sys
import time
import fcntl
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_retry

LOCK_FILE = os.path.expanduser("~/.hermes/calibration_audit.lock")
KANBAN_DB = db_retry.KANBAN_DB
PROMETHEUS_DB = db_retry.PROMETHEUS_DB


def acquire_lock():
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def release_lock(fd):
    if fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        fd.close()


def get_benchmark_results(kconn, pconn):
    """Get benchmark worker_results with calibrated_confidence and known answers."""
    # Get all benchmark worker_results
    wr_rows = pconn.execute("""
        SELECT wr.kanban_task_id, wr.hypothesis_supported, wr.calibrated_confidence, 
               wr.confidence, wr.id
        FROM worker_results wr
        WHERE wr.hypothesis_supported IS NOT NULL
        AND wr.calibrated_confidence IS NOT NULL
    """).fetchall()
    
    results = []
    for tid, supported, cal_conf, raw_conf, wr_id in wr_rows:
        if not tid:
            continue
        # Get CURIOSITY_ID from task body
        body_row = kconn.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()
        if not body_row or not body_row[0]:
            continue
        m = re.search(r'CURIOSITY_ID:\s*(\d+)', body_row[0])
        if not m:
            continue
        cid = int(m.group(1))
        
        # Get curiosity info
        crow = pconn.execute(
            "SELECT known_answer, benchmark_id, evidence_depth FROM curiosities WHERE id = ?",
            (cid,)
        ).fetchone()
        if not crow or not crow[1]:  # Not a benchmark question
            continue
        
        known_answer = crow[0]
        benchmark_id = crow[1]
        depth = crow[2] or 0
        
        # GROUND-TRUTH GUARD (fix 2026-06-25): only score rows whose known_answer is
        # actually typed 'true'/'false'. ~9,661 of ~11,900 benchmark curiosities have
        # known_answer=NULL; the old code scored every NULL row as if truth were "false"
        # (because `known_answer == "true"` is False for NULL), counting ~6,400
        # ground-truthless rows as wrong and dragging reported accuracy to ~0.45 /
        # Brier ~0.55 — a measurement artifact, not a model defect. On the typed rows
        # the model is acc~0.91. See architecture-changelog 2026-06-25 NULL-known_answer entry.
        if str(known_answer).strip().lower() not in ("true", "false"):
            continue
        
        # Compute correctness
        is_correct = (supported == 1) == (known_answer == "true")

        # AXIS CORRECTION (fix 2026-06-25): calibrated_confidence measures
        # P(hypothesis SUPPORTED), but for known_answer='false' questions the
        # correct worker action is to REFUTE, so a well-calibrated model SHOULD
        # report LOW support-confidence there. Scoring raw calibrated_confidence
        # against correctness on those rows falsely reads as "underconfident"
        # (verified 2026-06-25: known=false+low-conf n=1070 was 95% correct — the
        # model doubting a false hypothesis is RIGHT, not miscalibrated). So we
        # score every consumer against effective_confidence = confidence-in-the-
        # correct-direction: cal_conf for true questions, (1-cal_conf) for false.
        # This is computed ONCE here so Brier / reliability / bias can never
        # disagree about which axis they're on.
        effective_confidence = cal_conf if (known_answer == "true") else (1.0 - cal_conf)

        results.append({
            "wr_id": wr_id,
            "calibrated_confidence": cal_conf,
            "effective_confidence": effective_confidence,
            "raw_confidence": raw_conf,
            "hypothesis_supported": supported,
            "known_answer": known_answer,
            "is_correct": is_correct,
            "benchmark_id": benchmark_id,
            "depth": depth,
        })
    
    return results


def compute_brier_score(results):
    """Brier score: mean of (predicted_prob - actual_outcome)^2."""
    if not results:
        return None
    total = 0.0
    for r in results:
        predicted = r["effective_confidence"]
        actual = 1.0 if r["is_correct"] else 0.0
        total += (predicted - actual) ** 2
    return total / len(results)


def compute_reliability(results, n_bins=5):
    """Reliability: how well does predicted confidence match actual correctness rate?"""
    if not results:
        return None
    
    bins = [[] for _ in range(n_bins)]
    for r in results:
        conf = r["effective_confidence"]
        bin_idx = min(int(conf * n_bins), n_bins - 1)
        bins[bin_idx].append(r)
    
    reliability_data = []
    for i, bin_results in enumerate(bins):
        if not bin_results:
            continue
        avg_conf = sum(r["effective_confidence"] for r in bin_results) / len(bin_results)
        accuracy = sum(1 for r in bin_results if r["is_correct"]) / len(bin_results)
        reliability_data.append({
            "bin": i,
            "range": f"{i/n_bins:.1f}-{(i+1)/n_bins:.1f}",
            "count": len(bin_results),
            "avg_confidence": round(avg_conf, 3),
            "actual_accuracy": round(accuracy, 3),
            "gap": round(abs(avg_conf - accuracy), 3),
        })
    
    return reliability_data


def compute_bias(results):
    """Systematic bias: positive = overconfident, negative = underconfident."""
    if not results:
        return None
    avg_conf = sum(r["effective_confidence"] for r in results) / len(results)
    accuracy = sum(1 for r in results if r["is_correct"]) / len(results)
    return avg_conf - accuracy


def audit(kconn, pconn):
    """Run the full calibration audit."""
    results = get_benchmark_results(kconn, pconn)
    
    if not results:
        print("calibration_audit: no benchmark results with calibrated_confidence yet")
        return
    
    total = len(results)
    correct = sum(1 for r in results if r["is_correct"])
    accuracy = correct / total
    
    brier = compute_brier_score(results)
    bias = compute_bias(results)
    reliability = compute_reliability(results)
    
    print(f"=== CALIBRATION AUDIT (external ground truth) ===")
    print(f"Benchmark results: {total}")
    print(f"Actual accuracy: {accuracy:.1%} ({correct}/{total})")
    print(f"Avg effective_confidence (direction-corrected): {sum(r['effective_confidence'] for r in results)/total:.3f}")
    print(f"Brier score: {brier:.4f} (lower=better, 0=perfect)")
    print(f"Systematic bias: {bias:+.3f} ({'OVERCONFIDENT' if bias > 0 else 'UNDERCONFIDENT'})")
    
    if reliability:
        print(f"\nReliability by confidence bin:")
        print(f"  {'Range':<12} {'Count':<8} {'Avg Conf':<12} {'Actual Acc':<12} {'Gap':<8}")
        for b in reliability:
            print(f"  {b['range']:<12} {b['count']:<8} {b['avg_confidence']:<12} {b['actual_accuracy']:<12} {b['gap']:<8}")
    
    # Per-depth breakdown
    by_depth = {}
    for r in results:
        d = r["depth"]
        if d not in by_depth:
            by_depth[d] = []
        by_depth[d].append(r)
    
    if len(by_depth) > 1:
        print(f"\nBy evidence depth:")
        for d in sorted(by_depth.keys()):
            dr = by_depth[d]
            d_acc = sum(1 for r in dr if r["is_correct"]) / len(dr)
            d_conf = sum(r["effective_confidence"] for r in dr) / len(dr)
            print(f"  Depth {d}: accuracy={d_acc:.1%}, avg_eff_conf={d_conf:.3f}, n={len(dr)}")
    
    # Verdict
    if abs(bias) > 0.15:
        print(f"\n⚠ MISCALIBRATED: bias={bias:+.3f} (threshold: ±0.15)")
        print(f"  The calibration model {'overestimates' if bias > 0 else 'underestimates'} correctness.")
        print(f"  This is expected — it was trained on worker labels, not external truth.")
        print(f"  Feed benchmark data into calibration trainer to correct.")
    elif brier and brier > 0.15:
        print(f"\n⚠ POOR DISCRIMINATION: Brier={brier:.4f} (threshold: 0.15)")
        print(f"  The calibration model cannot distinguish correct from incorrect answers.")
    else:
        print(f"\n✓ Calibration looks reasonable on benchmark data.")
    
    # Save audit result for the calibration trainer to pick up
    audit_result = {
        "timestamp": time.time(),
        "total": total,
        "accuracy": accuracy,
        "brier_score": brier,
        "bias": bias,
        "reliability": reliability,
        "verdict": "MISCALIBRATED" if abs(bias) > 0.15 else ("POOR_DISCRIMINATION" if (brier and brier > 0.15) else "OK"),
    }
    audit_path = os.path.expanduser("~/.hermes/calibration_audit_result.json")
    with open(audit_path, "w") as f:
        json.dump(audit_result, f, indent=2)
    print(f"\nAudit result saved to {audit_path}")


def main():
    lock_fd = acquire_lock()
    if lock_fd is None:
        print("calibration_audit: another instance running, skipping")
        return
    
    try:
        kconn = db_retry.get_db(KANBAN_DB)
        pconn = db_retry.get_db(PROMETHEUS_DB)
        audit(kconn, pconn)
    except Exception as e:
        print(f"calibration_audit ERROR: {e}")
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
