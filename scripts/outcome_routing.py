from db_retry import get_db
#!/usr/bin/env python3
"""
outcome_routing.py — Outcome-aware + trust-aware edge scoring.

Computes four signals for every (source, target) domain pair:
  1. Flow weight:     traffic volume (existing)
  2. Outcome weight:  success_rate × log(1 + attempts)
  3. Novelty bonus:   1 / (1 + routing_count)
  4. Trust penalty:   penalizes overconfident domains, boosts underconfident

Combines them into a routing_score that replaces traffic-only scoring.

Usage:
    python3 outcome_routing.py                  # Print current scores
    python3 outcome_routing.py --json           # JSON output for scorer
    python3 outcome_routing.py --apply          # Write scores to routing_cache
    python3 outcome_routing.py --compare        # Compare old vs new scores
    python3 outcome_routing.py --trust          # Show trust scores per domain
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
CACHE_PATH = HERMES / "routing_outcome_cache.json"
LOG_PATH = HERMES / "routing_log_outcome.jsonl"

# Blending weights — defaults, overridden by auto-tune state
ALPHA = 0.25   # flow weight — reduced to break rich-get-richer bias (hidden bridges crisis)
BETA = 0.45    # outcome weight — increased to reward proven routes
GAMMA_DEFAULT = 0.15   # novelty bonus (default)
DELTA_DEFAULT = 0.15   # trust penalty weight (default)


def _load_tune_weights():
    """Load novelty/trust weights from auto-tune state if available."""
    try:
        state_path = Path(os.path.expanduser("~/.hermes/auto_tune_state.json"))
        if state_path.exists():
            with open(state_path) as f:
                state = json.load(f)
            return (
                state.get("novelty_weight", GAMMA_DEFAULT),
                state.get("trust_weight", DELTA_DEFAULT),
            )
    except Exception:
        pass
    return GAMMA_DEFAULT, DELTA_DEFAULT

# Exploration guardrails
MAX_EXPLOIT = 0.60   # never route more than 60% along known-successful edges
MIN_EXPLORE = 0.20   # always maintain 20% random exploration
MIN_TRANSFERS = 3    # need at least 3 transfers to compute outcome weight


def load_flow_weights():
    """Load existing edge weights from topology export."""
    if not EXPORT_PATH.exists():
        return {}
    with open(EXPORT_PATH) as f:
        topo = json.load(f)
    weights = {}
    for edge in topo.get("edges", []):
        src = edge.get("source", "")
        tgt = edge.get("target", "")
        w = edge.get("weight", 0)
        if src and tgt:
            weights[(src, tgt)] = w
    return weights


def compute_outcome_weights():
    """Compute outcome weights from worker_results."""
    conn = get_db(str(DB_PATH))

    rows = conn.execute("""
        SELECT e.domain, wr.domain,
               CASE WHEN wr.key_finding LIKE '%CONFIRMED%'
                         OR wr.key_finding LIKE '%SUPPORTED%'
                    THEN 1.0
                    WHEN wr.key_finding LIKE '%REFUTED%'
                    THEN 0.0
                    ELSE NULL END as success
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

    outcome_weights = {}
    for (src, tgt), stats in pair_data.items():
        attempts = stats["attempts"]
        if attempts >= MIN_TRANSFERS:
            success_rate = stats["successes"] / attempts
            # Outcome weight: success_rate scaled by confidence (log of attempts)
            outcome_weights[(src, tgt)] = success_rate * math.log(1 + attempts)

    return outcome_weights, pair_data


def compute_novelty_bonus(routing_log_path=None):
    """Compute novelty bonus: edges that haven't been explored much get higher bonus."""
    # Count how many times each edge has been routed through
    routing_counts = defaultdict(int)

    if routing_log_path and os.path.exists(routing_log_path):
        with open(routing_log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    src = entry.get("source", "")
                    tgt = entry.get("target", "")
                    if src and tgt:
                        routing_counts[(src, tgt)] += 1
                except json.JSONDecodeError:
                    pass

    # Also count from worker_results (historical routing)
    conn = get_db(str(DB_PATH))
    rows = conn.execute("""
        SELECT e.domain, wr.domain
        FROM worker_results wr
        JOIN experiments e ON wr.experiment_id = e.id
        WHERE e.domain IS NOT NULL AND wr.domain IS NOT NULL
          AND e.domain != wr.domain
          AND e.domain != 'general' AND wr.domain != 'general'
    """).fetchall()
    conn.close()

    for src, tgt in rows:
        routing_counts[(src, tgt)] += 1

    novelty = {}
    for edge, count in routing_counts.items():
        novelty[edge] = 1.0 / (1 + count)

    return novelty


def compute_trust_scores():
    """Compute per-domain trust scores from confidence vs actual outcomes.
    
    Trust = actual_accuracy - average_confidence
    Negative = overconfident (says sure, is wrong)
    Positive = underconfident (says unsure, is right)
    """
    conn = get_db(str(DB_PATH))
    rows = conn.execute("""
        SELECT wr.domain, wr.confidence,
               CASE WHEN wr.key_finding LIKE '%CONFIRMED%'
                         OR wr.key_finding LIKE '%SUPPORTED%'
                    THEN 1.0
                    WHEN wr.key_finding LIKE '%REFUTED%'
                    THEN 0.0
                    ELSE NULL END as success
        FROM worker_results wr
        WHERE wr.confidence IS NOT NULL
          AND wr.key_finding IS NOT NULL
          AND (wr.key_finding LIKE '%CONFIRMED%'
               OR wr.key_finding LIKE '%SUPPORTED%'
               OR wr.key_finding LIKE '%REFUTED%')
    """).fetchall()
    conn.close()

    domain_data = defaultdict(lambda: {"conf": [], "success": []})
    for domain, conf, success in rows:
        if success is not None:
            # Skip non-numeric confidence (empty strings in DB)
            try:
                conf_f = float(conf)
            except (TypeError, ValueError):
                continue
            domain_data[domain]["conf"].append(conf_f)
            domain_data[domain]["success"].append(success)

    trust_scores = {}
    for domain, data in domain_data.items():
        n = len(data["conf"])
        if n < 3:
            continue
        avg_conf = sum(data["conf"]) / n
        avg_acc = sum(data["success"]) / n
        trust = avg_acc - avg_conf
        trust_scores[domain] = {
            "trust": trust,
            "avg_confidence": avg_conf,
            "actual_accuracy": avg_acc,
            "n": n,
        }

    return trust_scores


def compute_routing_scores():
    """Combine flow, outcome, novelty, and trust into routing scores."""
    flow_weights = load_flow_weights()
    outcome_weights, pair_data = compute_outcome_weights()
    novelty_bonus = compute_novelty_bonus(LOG_PATH)
    trust_scores = compute_trust_scores()
    GAMMA, DELTA = _load_tune_weights()

    # Normalize flow weights to 0-1
    max_flow = max(flow_weights.values()) if flow_weights else 1
    max_outcome = max(outcome_weights.values()) if outcome_weights else 1

    all_edges = set(flow_weights.keys()) | set(outcome_weights.keys())

    scores = {}
    for edge in all_edges:
        src, tgt = edge
        flow = flow_weights.get(edge, 0) / max_flow
        outcome = outcome_weights.get(edge, 0) / max_outcome
        novelty = novelty_bonus.get(edge, 0)

        # Trust adjustment: penalize overconfident domains, boost underconfident
        # Use the trust score of the target domain (where the finding lands)
        target_trust = trust_scores.get(tgt, {}).get("trust", 0)
        # Clamp trust to [-0.5, 0.5] to prevent extreme adjustments
        target_trust = max(-0.5, min(0.5, target_trust))
        trust_adjustment = DELTA * target_trust

        stats = pair_data.get(edge, {"successes": 0, "attempts": 0})
        success_rate = stats["successes"] / stats["attempts"] if stats["attempts"] > 0 else 0

        # GRADUATED DEAD-ZONE PENALTY: edges with few or no attempts have untested
        # traffic flow. Penalize their flow weight to redirect traffic toward
        # proven routes (3+ attempts with high success rate).
        #
        # Without this, 88%+ of traffic accumulates on edges with <3 attempts
        # (2026-06-20: 95 edges with 1 attempt consume 42.6% of total flow,
        # while 31/40 hidden bridges with 3+ attempts get ZERO flow).
        #
        # Graduated penalty based on confirming data:
        #   0 attempts: 0.10 (10x reduction) — completely untested
        #   1 attempt:  0.20 (5x reduction)  — one data point, unreliable
        #   2 attempts: 0.50 (2x reduction)  — two data points, moderately reliable
        #   3+ attempts: 1.00 (no penalty)   — enough confirming data
        #
        # Monitor: if zero-flow drops below 60%, ease the 1-attempt penalty to 0.30.
        # If routing collapse approaches 95%, halve all penalties (0.20/0.40/0.75).
        if stats["attempts"] == 0:
            flow *= 0.10   # 10x reduction — completely untested
        elif stats["attempts"] == 1:
            flow *= 0.20   # 5x reduction — one unconfirmed result
        elif stats["attempts"] == 2:
            flow *= 0.50   # 2x reduction — barely tested

        routing_score = ALPHA * flow + BETA * outcome + GAMMA * novelty + trust_adjustment

        # Success-rate floor: proven routes (≥80% SR, ≥3 attempts) get a minimum
        # routing score to prevent them from becoming "hidden bridges" — routes that
        # work but can never be discovered because they have no flow weight.
        # Dynamic floor: scales with success_rate and log(1 + attempts) to give
        # high-confidence proven routes a bigger boost than marginal ones.
        if success_rate >= 0.80 and stats["attempts"] >= 3:
            import math
            # Base floor: 0.45, boosted by success_rate and attempt confidence
            base_floor = 0.45
            sr_boost = (success_rate - 0.80) * 0.5   # up to +0.10 for 100% SR
            attempt_boost = math.log(1 + stats["attempts"]) / 10  # scales with attempts: 4→0.16, 10→0.24, 20→0.31
            dynamic_floor = base_floor + sr_boost + attempt_boost
            routing_score = max(routing_score, dynamic_floor)

        scores[edge] = {
            "source": src,
            "target": tgt,
            "flow_raw": flow_weights.get(edge, 0),
            "flow_norm": round(flow, 4),
            "outcome_norm": round(outcome, 4),
            "novelty": round(novelty, 4),
            "trust_penalty": round(trust_adjustment, 4),
            "target_trust": round(target_trust, 4),
            "routing_score": round(routing_score, 4),
            "success_rate": round(success_rate, 3),
            "attempts": stats["attempts"],
        }

    return scores


def log_routing_decision(source, target, old_score, new_score, decision):
    """Log a routing decision for later analysis."""
    entry = {
        "timestamp": time.time(),
        "source": source,
        "target": target,
        "old_score": old_score,
        "new_score": new_score,
        "decision": decision,
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def apply_scores():
    """Write computed scores to cache file for use by curiosity scorer."""
    scores = compute_routing_scores()
    cache = {}
    for edge, data in scores.items():
        key = f"{data['source']}->{data['target']}"
        cache[key] = data

    # Atomic write: dump to temp file, then rename to avoid race conditions
    # where readers see an empty/truncated file mid-write.
    import tempfile
    fd, tmp_path = tempfile.mkstemp(dir=CACHE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp_path, CACHE_PATH)
    except Exception:
        os.unlink(tmp_path)
        raise

    return len(cache)


def compare_old_vs_new():
    """Compare traffic-only ranking vs outcome-aware ranking."""
    scores = compute_routing_scores()

    # Sort by flow (old) vs routing_score (new)
    by_flow = sorted(scores.items(), key=lambda x: x[1]["flow_raw"], reverse=True)
    by_routing = sorted(scores.items(), key=lambda x: x[1]["routing_score"], reverse=True)

    # Find rank changes
    flow_rank = {edge: i for i, (edge, _) in enumerate(by_flow)}
    routing_rank = {edge: i for i, (edge, _) in enumerate(by_routing)}

    print(f"{'='*80}")
    print(f"OLD vs NEW RANKING COMPARISON")
    print(f"{'='*80}")
    print(f"{'Source':<28} {'Target':<28} {'Flow Rank':>9} {'New Rank':>9} {'Delta':>6} {'Success':>8}")
    print(f"{'-'*28} {'-'*28} {'-'*9} {'-'*9} {'-'*6} {'-'*8}")

    divergences = []
    for edge in scores:
        fr = flow_rank[edge]
        rr = routing_rank[edge]
        delta = fr - rr
        divergences.append((edge, fr, rr, delta, scores[edge]))

    # Show biggest gainers (moved UP in new ranking)
    divergences.sort(key=lambda x: x[3], reverse=True)
    print(f"\n  BIGGEST GAINERS (promoted by outcome-aware routing):")
    for edge, fr, rr, delta, data in divergences[:10]:
        if delta > 0:
            print(f"  {data['source']:<28} {data['target']:<28} {fr:>9} {rr:>9} {delta:>+6} {data['success_rate']:>7.0%}")

    # Show biggest losers (moved DOWN in new ranking)
    print(f"\n  BIGGEST LOSERS (demoted by outcome-aware routing):")
    for edge, fr, rr, delta, data in divergences[-10:]:
        if delta < 0:
            print(f"  {data['source']:<28} {data['target']:<28} {fr:>9} {rr:>9} {delta:>+6} {data['success_rate']:>7.0%}")

    return divergences


def main():
    parser = argparse.ArgumentParser(description="Outcome-aware routing scores")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--apply", action="store_true", help="Write scores to cache")
    parser.add_argument("--compare", action="store_true", help="Compare old vs new rankings")
    parser.add_argument("--trust", action="store_true", help="Show trust scores per domain")
    args = parser.parse_args()

    # Load GAMMA/DELTA for display in main() (loaded inside compute_routing_scores but not accessible here)
    GAMMA, DELTA = _load_tune_weights()

    if args.trust:
        trust = compute_trust_scores()
        sorted_trust = sorted(trust.items(), key=lambda x: x[1]["trust"])
        print(f"{'='*70}")
        print(f"DOMAIN TRUST SCORES (confidence minus accuracy)")
        print(f"{'='*70}")
        print(f"{'Domain':<40} {'Trust':>7} {'Conf':>6} {'Acc':>6} {'N':>5}")
        print(f"{'-'*40} {'-'*7} {'-'*6} {'-'*6} {'-'*5}")
        for domain, data in sorted_trust[:15]:
            t = data["trust"]
            marker = " ⚠️" if t < -0.10 else ""
            print(f"  {domain:<40} {t:>+7.3f} {data['avg_confidence']:>5.0%} {data['actual_accuracy']:>5.0%} {data['n']:>5}{marker}")
        print(f"\n  ...")
        for domain, data in sorted_trust[-5:]:
            t = data["trust"]
            print(f"  {domain:<40} {t:>+7.3f} {data['avg_confidence']:>5.0%} {data['actual_accuracy']:>5.0%} {data['n']:>5}")
        
        over = sum(1 for _, d in trust.items() if d["trust"] < -0.10)
        cal = sum(1 for _, d in trust.items() if -0.10 <= d["trust"] <= 0.10)
        under = sum(1 for _, d in trust.items() if d["trust"] > 0.10)
        print(f"\n  Overconfident: {over} | Calibrated: {cal} | Underconfident: {under}")
        return

    if args.apply:
        n = apply_scores()
        print(f"Wrote {n} edge scores to {CACHE_PATH}")
        return

    if args.compare:
        compare_old_vs_new()
        return

    scores = compute_routing_scores()

    if args.json:
        output = {f"{d['source']}->{d['target']}": d for d in scores.values()}
        print(json.dumps(output, indent=2))
    else:
        # Print summary
        sorted_scores = sorted(scores.values(), key=lambda d: d["routing_score"], reverse=True)
        print(f"{'='*80}")
        print(f"OUTCOME-AWARE ROUTING SCORES")
        print(f"{'='*80}")
        print(f"Total edges: {len(scores)}")
        print(f"Blending: α={ALPHA} (flow) + β={BETA} (outcome) + γ={GAMMA} (novelty) + δ={DELTA} (trust)")
        print(f"\nTop 20 edges by routing score:")
        print(f"  {'Source':<28} {'Target':<28} {'Score':>7} {'Flow':>5} {'Out':>5} {'Nov':>4} {'Trust':>6} {'Succ':>5} {'N':>4}")
        print(f"  {'-'*28} {'-'*28} {'-'*7} {'-'*5} {'-'*5} {'-'*4} {'-'*6} {'-'*5} {'-'*4}")
        for d in sorted_scores[:20]:
            print(f"  {d['source']:<28} {d['target']:<28} {d['routing_score']:>7.3f} {d['flow_norm']:>5.3f} {d['outcome_norm']:>5.3f} {d['novelty']:>4.3f} {d['trust_penalty']:>+5.3f} {d['success_rate']:>4.0%} {d['attempts']:>4}")

        print(f"\nBottom 10 edges (lowest routing score):")
        for d in sorted_scores[-10:]:
            print(f"  {d['source']:<28} {d['target']:<28} {d['routing_score']:>7.3f} {d['flow_norm']:>5.3f} {d['outcome_norm']:>5.3f} {d['novelty']:>4.3f} {d['trust_penalty']:>+5.3f} {d['success_rate']:>4.0%} {d['attempts']:>4}")


if __name__ == "__main__":
    main()
