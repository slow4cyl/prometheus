from db_retry import get_db
#!/usr/bin/env python3
"""
calibration_test.py — Rerunnable topology calibration analysis.

Measures whether edge weights correlate with actual transfer success.
Compares flow-only vs outcome-aware routing scores.

Usage:
    python3 calibration_test.py                # Full analysis
    python3 calibration_test.py --quick        # Just the correlation numbers
    python3 calibration_test.py --before-after # Compare two runs (reads log files)
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

HERMES = Path(os.path.expanduser("~/.hermes"))
DB_PATH = HERMES / "prometheus.db"
EXPORT_PATH = HERMES / "topology_full_export.json"
OUTCOME_CACHE = HERMES / "routing_outcome_cache.json"
BASELINE_LOG = HERMES / "calibration_baseline.json"

MIN_TRANSFERS = 3
MIN_SUCCESS_RATE_TRANFERS = 5  # for discovery map


def pearson(x, y):
    n = len(x)
    if n < 3:
        return 0
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y)) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x) / n)
    sy = math.sqrt(sum((b - my) ** 2 for b in y) / n)
    return cov / (sx * sy) if sx > 0 and sy > 0 else 0


def spearman(x, y):
    def rankify(lst):
        indexed = sorted(range(len(lst)), key=lambda i: lst[i])
        ranks = [0] * len(lst)
        for r, i in enumerate(indexed, 1):
            ranks[i] = r
        return ranks
    return pearson(rankify(x), rankify(y))


def load_flow_weights():
    if not EXPORT_PATH.exists():
        return {}
    with open(EXPORT_PATH) as f:
        topo = json.load(f)
    weights = {}
    for edge in topo.get("edges", []):
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if src and tgt:
            weights[(src, tgt)] = edge.get("weight", 0)
    return weights


def load_outcome_weights():
    if not OUTCOME_CACHE.exists():
        return {}
    with open(OUTCOME_CACHE) as f:
        cache = json.load(f)
    weights = {}
    for key, data in cache.items():
        parts = key.split("->")
        if len(parts) == 2:
            weights[(parts[0], parts[1])] = data.get("routing_score", 0)
    return weights


def load_transfer_data():
    conn = get_db(str(DB_PATH))
    rows = conn.execute("""
        SELECT e.domain, wr.domain,
               CASE WHEN wr.key_finding LIKE '%CONFIRMED%'
                         OR wr.key_finding LIKE '%SUPPORTED%'
                    THEN 1.0
                    WHEN wr.key_finding LIKE '%REFUTED%'
                    THEN 0.0
                    ELSE NULL END
        FROM worker_results wr
        JOIN experiments e ON wr.experiment_id = e.id
        WHERE e.domain IS NOT NULL AND wr.domain IS NOT NULL
          AND e.domain != wr.domain
          AND e.domain != 'general' AND wr.domain != 'general'
          AND wr.key_finding IS NOT NULL
          AND (wr.key_finding LIKE '%CONFIRMED%'
               OR wr.key_finding LIKE '%SUPPORTED%'
               OR wr.key_finding LIKE '%REFUTED%')
    """).fetchall()
    conn.close()

    pair_data = defaultdict(lambda: {"successes": 0, "attempts": 0})
    for src, tgt, success in rows:
        pair_data[(src, tgt)]["attempts"] += 1
        if success is not None:
            pair_data[(src, tgt)]["successes"] += success

    return pair_data, len(rows)


def run_calibration(weights, pair_data, label=""):
    """Run calibration test: correlate edge weights with success rates."""
    max_w = max(weights.values()) if weights else 1
    data_points = []
    for edge, stats in pair_data.items():
        if edge in weights and stats["attempts"] >= MIN_TRANSFERS:
            w = weights[edge] / max_w
            sr = stats["successes"] / stats["attempts"]
            data_points.append({"weight": w, "success_rate": sr, "edge": edge,
                                "attempts": stats["attempts"]})

    if len(data_points) < 10:
        return {"r": 0, "n": len(data_points), "label": label}

    ws = [d["weight"] for d in data_points]
    rs = [d["success_rate"] for d in data_points]

    r_p = pearson(ws, rs)
    r_s = spearman(ws, rs)

    return {
        "r": r_p,
        "spearman": r_s,
        "n": len(data_points),
        "label": label,
        "data_points": data_points,
    }


def run_baselines(pair_data):
    """Compute baseline correlations."""
    # Volume baseline: source domain experiment count
    conn = get_db(str(DB_PATH))
    domain_counts = dict(conn.execute(
        "SELECT domain, COUNT(*) FROM experiments WHERE domain IS NOT NULL GROUP BY domain"
    ).fetchall())
    conn.close()

    # Target popularity
    target_counts = defaultdict(int)
    for (src, tgt), stats in pair_data.items():
        target_counts[tgt] += stats["attempts"]

    data_points = []
    for edge, stats in pair_data.items():
        if stats["attempts"] >= MIN_TRANSFERS:
            sr = stats["successes"] / stats["attempts"]
            data_points.append({
                "source_vol": domain_counts.get(edge[0], 0),
                "target_pop": target_counts.get(edge[1], 0),
                "success_rate": sr,
            })

    if len(data_points) < 10:
        return {}

    source_vols = [d["source_vol"] for d in data_points]
    target_pops = [d["target_pop"] for d in data_points]
    success_rates = [d["success_rate"] for d in data_points]

    return {
        "volume": pearson(source_vols, success_rates),
        "popularity": pearson(target_pops, success_rates),
    }


def find_hidden_bridges(weights, pair_data, flow_weights):
    """Find edges with high success but low weight."""
    max_flow = max(flow_weights.values()) if flow_weights else 1
    hidden = []
    for edge, stats in pair_data.items():
        if stats["attempts"] >= MIN_SUCCESS_RATE_TRANFERS:
            sr = stats["successes"] / stats["attempts"]
            fw = flow_weights.get(edge, 0) / max_flow
            if sr >= 0.80 and fw < 0.05:
                hidden.append({"edge": edge, "success_rate": sr, "flow": fw, "n": stats["attempts"]})
    return sorted(hidden, key=lambda d: (-d["success_rate"], -d["n"]))


def find_anti_bridges(flow_weights, pair_data):
    """Find edges with high traffic but low success."""
    max_flow = max(flow_weights.values()) if flow_weights else 1
    anti = []
    for edge, stats in pair_data.items():
        if stats["attempts"] >= MIN_SUCCESS_RATE_TRANFERS:
            sr = stats["successes"] / stats["attempts"]
            fw = flow_weights.get(edge, 0) / max_flow
            if fw > 0.5 and sr < 0.60:
                anti.append({"edge": edge, "success_rate": sr, "flow": fw, "n": stats["attempts"]})
    return sorted(anti, key=lambda d: d["success_rate"])


def main():
    parser = argparse.ArgumentParser(description="Topology calibration test")
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--before-after", action="store_true")
    args = parser.parse_args()

    t0 = time.time()

    # Load data
    flow_weights = load_flow_weights()
    outcome_weights = load_outcome_weights()
    pair_data, total_records = load_transfer_data()

    total_successes = sum(d["successes"] for d in pair_data.values())
    total_attempts = sum(d["attempts"] for d in pair_data.values())
    base_rate = total_successes / total_attempts if total_attempts > 0 else 0

    # Run calibrations
    flow_cal = run_calibration(flow_weights, pair_data, "Flow (traffic)")
    outcome_cal = run_calibration(outcome_weights, pair_data, "Outcome-aware")

    # Baselines
    baselines = run_baselines(pair_data)

    # Hidden/anti bridges
    hidden = find_hidden_bridges(outcome_weights, pair_data, flow_weights)
    anti = find_anti_bridges(flow_weights, pair_data)

    elapsed = time.time() - t0

    if args.quick:
        print(f"Flow r={flow_cal['r']:.4f}  Outcome r={outcome_cal['r']:.4f}  Base={base_rate:.1%}  ({elapsed:.1f}s)")
        return

    print(f"{'='*70}")
    print(f"TOPOLOGY CALIBRATION TEST")
    print(f"{'='*70}")
    print(f"  Total cross-domain records: {total_records:,}")
    print(f"  Unique (source,target) pairs: {len(pair_data):,}")
    print(f"  Pairs with >={MIN_TRANSFERS} transfers: {flow_cal['n']}")
    print(f"  Base success rate: {base_rate:.1%}")

    print(f"\n{'='*70}")
    print(f"CALIBRATION SCORES")
    print(f"{'='*70}")
    print(f"  {'Method':<25} {'Pearson r':>10} {'Spearman ρ':>11} {'N':>6}")
    print(f"  {'-'*25} {'-'*10} {'-'*11} {'-'*6}")
    print(f"  {'Flow (traffic only)':<25} {flow_cal['r']:>+10.4f} {flow_cal.get('spearman', 0):>+11.4f} {flow_cal['n']:>6}")
    if outcome_cal["n"] > 0:
        print(f"  {'Outcome-aware':<25} {outcome_cal['r']:>+10.4f} {outcome_cal.get('spearman', 0):>+11.4f} {outcome_cal['n']:>6}")

    if baselines:
        print(f"\n  BASELINES:")
        print(f"  {'Source domain volume':<25} {baselines.get('volume', 0):>+10.4f}")
        print(f"  {'Target domain popularity':<25} {baselines.get('popularity', 0):>+11.4f}")
        print(f"  {'Random':<25} {'~0.00':>10}")

    # Determine winner
    all_scores = {"flow": flow_cal["r"]}
    if outcome_cal["n"] > 0:
        all_scores["outcome"] = outcome_cal["r"]
    for k, v in baselines.items():
        all_scores[k] = v

    best = max(all_scores, key=all_scores.get)
    print(f"\n  WINNER: {best} (r={all_scores[best]:+.4f})")

    if outcome_cal["n"] > 0:
        improvement = outcome_cal["r"] - flow_cal["r"]
        print(f"  Improvement: {improvement:+.4f}")

    # Bridges
    print(f"\n{'='*70}")
    print(f"HIDDEN BRIDGES (high success, low traffic): {len(hidden)}")
    print(f"{'='*70}")
    if hidden:
        print(f"  {'Source':<28} {'Target':<28} {'Success':>8} {'Flow':>6} {'N':>4}")
        print(f"  {'-'*28} {'-'*28} {'-'*8} {'-'*6} {'-'*4}")
        for d in hidden[:10]:
            e = d["edge"]
            print(f"  {e[0]:<28} {e[1]:<28} {d['success_rate']:>7.0%} {d['flow']:>6.3f} {d['n']:>4}")

    print(f"\n{'='*70}")
    print(f"ANTI-BRIDGES (high traffic, low success): {len(anti)}")
    print(f"{'='*70}")
    if anti:
        print(f"  {'Source':<28} {'Target':<28} {'Success':>8} {'Flow':>6} {'N':>4}")
        print(f"  {'-'*28} {'-'*28} {'-'*8} {'-'*6} {'-'*4}")
        for d in anti[:10]:
            e = d["edge"]
            print(f"  {e[0]:<28} {e[1]:<28} {d['success_rate']:>7.0%} {d['flow']:>6.3f} {d['n']:>4}")

    # Save baseline for comparison
    baseline = {
        "timestamp": time.time(),
        "flow_r": flow_cal["r"],
        "outcome_r": outcome_cal["r"] if outcome_cal["n"] > 0 else None,
        "base_rate": base_rate,
        "hidden_bridges": len(hidden),
        "anti_bridges": len(anti),
        "total_records": total_records,
    }
    with open(BASELINE_LOG, "w") as f:
        json.dump(baseline, f, indent=2)
    print(f"\n  Baseline saved to {BASELINE_LOG}")

    print(f"\n  Completed in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
