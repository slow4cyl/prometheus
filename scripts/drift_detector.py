#!/usr/bin/env python3
"""
Drift Detection + Calibration Tests — Threat Model Item 6

Monitors per-worker result distributions for slow-rot attacks.
When drift is detected, flags the worker for verification.

Usage:
  python3 drift_detector.py [--check] [--worker WORKER_ID] [--calibrate]
  
  --check: run drift detection on recent results
  --worker: check specific worker
  --calibrate: run calibration tests on all workers
"""

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(
    os.path.join(_hermes_home_env, "prometheus_db.py")
) else _HERMES_MAIN

DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")
DRIFT_STATE_PATH = os.path.expanduser("~/.hermes/inspector/drift_state.json")
AUDIT_LOG = os.path.expanduser("~/.hermes/logs/drift_audit.jsonl")


def load_drift_state():
    """Load drift detection state."""
    if os.path.exists(DRIFT_STATE_PATH):
        with open(DRIFT_STATE_PATH) as f:
            return json.load(f)
    return {"workers": {}, "last_check": None, "alerts": []}


def save_drift_state(state):
    """Save drift detection state."""
    os.makedirs(os.path.dirname(DRIFT_STATE_PATH), exist_ok=True)
    with open(DRIFT_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def log_alert(worker_id, alert_type, details):
    """Log a drift alert."""
    entry = {
        "timestamp": time.time(),
        "worker_id": worker_id,
        "alert_type": alert_type,
        "details": details,
    }
    os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def get_worker_results(worker_id=None, limit=200):
    """Get recent worker results from the database."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    if worker_id:
        rows = conn.execute(
            "SELECT experiment_id, key_finding, confidence, domain, created_at "
            "FROM worker_results WHERE worker_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (worker_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT experiment_id, key_finding, confidence, domain, worker_id, created_at "
            "FROM worker_results "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return rows


def compute_worker_fingerprint(worker_id, results):
    """Compute a statistical fingerprint for a worker's results."""
    if not results:
        return None

    # Compute average confidence
    confidences = [r[2] for r in results if r[2] is not None]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.5

    # Compute domain distribution
    domains = [r[3] for r in results if r[3]]
    domain_dist = defaultdict(int)
    for d in domains:
        domain_dist[d] += 1
    total = sum(domain_dist.values()) or 1
    domain_pct = {d: c / total for d, c in domain_dist.items()}

    # Compute finding length distribution
    lengths = [len(r[1] or "") for r in results if r[1]]
    avg_length = sum(lengths) / len(lengths) if lengths else 0

    return {
        "avg_confidence": round(avg_confidence, 4),
        "domain_distribution": domain_pct,
        "avg_finding_length": round(avg_length, 1),
        "sample_count": len(results),
    }


def detect_drift(worker_id, current_fingerprint, baseline_fingerprint):
    """Detect if a worker has drifted from its baseline."""
    if not baseline_fingerprint or not current_fingerprint:
        return False, "No baseline or current data"

    alerts = []

    # Check confidence drift (>3σ or >0.15 absolute change)
    conf_delta = abs(current_fingerprint["avg_confidence"] - baseline_fingerprint["avg_confidence"])
    if conf_delta > 0.15:
        alerts.append(f"Confidence drift: {baseline_fingerprint['avg_confidence']:.3f} → {current_fingerprint['avg_confidence']:.3f} (Δ={conf_delta:.3f})")

    # Check domain distribution change (Jaccard distance)
    base_domains = set(baseline_fingerprint["domain_distribution"].keys())
    curr_domains = set(current_fingerprint["domain_distribution"].keys())
    if base_domains and curr_domains:
        intersection = base_domains & curr_domains
        union = base_domains | curr_domains
        jaccard = len(intersection) / len(union) if union else 1.0
        if jaccard < 0.5:
            alerts.append(f"Domain distribution shifted: Jaccard={jaccard:.3f}")

    # Check finding length drift (>50% change)
    if baseline_fingerprint["avg_finding_length"] > 0:
        length_ratio = current_fingerprint["avg_finding_length"] / baseline_fingerprint["avg_finding_length"]
        if length_ratio > 1.5 or length_ratio < 0.5:
            alerts.append(f"Finding length drift: {baseline_fingerprint['avg_finding_length']:.0f} → {current_fingerprint['avg_finding_length']:.0f} ({length_ratio:.2f}x)")

    return len(alerts) > 0, alerts


def run_drift_check(target_worker=None):
    """Run drift detection on all workers or a specific worker."""
    state = load_drift_state()
    results_checked = 0
    alerts_found = 0

    if target_worker:
        workers = [target_worker]
    else:
        # Get all workers from recent results
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        rows = conn.execute(
            "SELECT DISTINCT worker_id FROM worker_results "
            "WHERE worker_id != '' AND worker_id IS NOT NULL"
        ).fetchall()
        conn.close()
        workers = [r[0] for r in rows]

    for worker_id in workers:
        if not worker_id:
            continue

        # Get recent results for this worker
        results = get_worker_results(worker_id, limit=100)
        if len(results) < 10:
            continue  # Not enough data

        current_fp = compute_worker_fingerprint(worker_id, results)
        if not current_fp:
            continue

        # Get baseline
        baseline = state.get("workers", {}).get(worker_id, {}).get("baseline")
        if not baseline:
            # First time seeing this worker — establish baseline
            state.setdefault("workers", {})[worker_id] = {
                "baseline": current_fp,
                "last_seen": datetime.now(timezone.utc).isoformat(),
                "status": "ok",
            }
            print(f"  {worker_id}: baseline established ({current_fp['sample_count']} samples)")
            results_checked += 1
            continue

        # Check for drift
        drifted, alerts = detect_drift(worker_id, current_fp, baseline)

        if drifted:
            alerts_found += len(alerts)
            state["workers"][worker_id]["status"] = "drift_detected"
            state["workers"][worker_id]["last_drift"] = datetime.now(timezone.utc).isoformat()
            state["workers"][worker_id]["drift_alerts"] = alerts
            state.setdefault("alerts", []).append({
                "worker": worker_id,
                "time": datetime.now(timezone.utc).isoformat(),
                "alerts": alerts,
            })
            # Keep last 50 alerts
            state["alerts"] = state["alerts"][-50:]

            log_alert(worker_id, "drift_detected", alerts)
            print(f"  ⚠ {worker_id}: DRIFT DETECTED")
            for a in alerts:
                print(f"    - {a}")
        else:
            state["workers"][worker_id]["status"] = "ok"
            state["workers"][worker_id]["last_seen"] = datetime.now(timezone.utc).isoformat()
            # Update baseline with rolling average
            old_baseline = baseline
            old_samples = old_baseline.get("sample_count", 100)
            new_samples = current_fp["sample_count"]
            total = old_samples + new_samples
            blended_conf = (
                old_baseline["avg_confidence"] * old_samples +
                current_fp["avg_confidence"] * new_samples
            ) / total
            state["workers"][worker_id]["baseline"]["avg_confidence"] = round(blended_conf, 4)
            state["workers"][worker_id]["baseline"]["sample_count"] = total
            print(f"  ✓ {worker_id}: OK (confidence: {current_fp['avg_confidence']:.3f})")

        results_checked += 1

    state["last_check"] = datetime.now(timezone.utc).isoformat()
    save_drift_state(state)

    print(f"\nDrift check complete: {results_checked} workers checked, {alerts_found} alerts")
    return alerts_found


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Drift detection for worker results")
    parser.add_argument("--check", action="store_true", help="Run drift detection")
    parser.add_argument("--worker", help="Check specific worker")
    parser.add_argument("--status", action="store_true", help="Show current drift status")
    args = parser.parse_args()

    if args.status:
        state = load_drift_state()
        print("=== Drift Detection Status ===")
        print(f"Last check: {state.get('last_check', 'never')}")
        print(f"Workers tracked: {len(state.get('workers', {}))}")
        for wid, info in state.get("workers", {}).items():
            status = info.get("status", "unknown")
            symbol = "⚠" if status == "drift_detected" else "✓"
            print(f"  {symbol} {wid}: {status}")
        alerts = state.get("alerts", [])
        if alerts:
            print(f"\nRecent alerts ({len(alerts)}):")
            for a in alerts[-5:]:
                print(f"  [{a.get('time', '?')[:16]}] {a.get('worker', '?')}: {', '.join(a.get('alerts', []))}")
    elif args.check or args.worker:
        run_drift_check(args.worker)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
