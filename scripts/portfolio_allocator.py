#!/usr/bin/env python3
"""
portfolio_allocator.py — Multi-objective budget allocation.

Dynamically adjusts weights across P(confirm), P(novel), P(expand) based on
current system state. Replaces the fixed 0.5/0.3/0.2 weighting with an
adaptive allocator that shifts budget toward under-served objectives.

Architecture:
  System State → Allocation Weights → Curiosity Scoring → Task Selection

The allocator answers: "What should we optimize for RIGHT NOW?"

Usage:
    python3 portfolio_allocator.py              # Show current allocation
    python3 portfolio_allocator.py --apply      # Apply to curiosities table
    python3 portfolio_allocator.py --history    # Show allocation history
    python3 portfolio_allocator.py --json       # JSON output for other scripts
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")
STATE_PATH = os.path.expanduser("~/.hermes/portfolio_state.json")
LOCK_PATH = os.path.expanduser("~/.hermes/.portfolio_allocator.lock")

# --- Target rates (what we want the system to maintain) ---
TARGETS = {
    "confirm": 0.40,    # 40% confirmation rate (exploitation floor)
    "novel": 0.15,      # 15% novelty rate (exploration floor)
    "expand": 0.30,     # 30% of experiments should reach new/underrepresented domains
    "break": 0.15,      # 15% of validated findings should reveal structural breaks
}

# --- Weight bounds ---
MIN_WEIGHT = 0.10      # No objective gets less than 10%
MAX_WEIGHT = 0.60      # No objective gets more than 60%
STEP_SIZE = 0.05       # Max weight change per cycle (prevents oscillation)


def get_db():
    from db_retry import get_db as _get_db
    return _get_db(DB_PATH)


def assess_state(conn, window_hours=24):
    """Measure current portfolio state over recent window."""
    cutoff = time.time() - (window_hours * 3600)
    
    # Confirmation rate
    row = conn.execute("""
        SELECT 
            SUM(CASE WHEN refutation_type='SUPPORTED' THEN 1 ELSE 0 END) as supported,
            COUNT(*) as total
        FROM experiments 
        WHERE refutation_type IS NOT NULL AND created_at > ?
    """, (cutoff,)).fetchone()
    confirm_rate = row[0] / max(row[1], 1)
    
    # Novelty rate (DISCOVERY tag)
    row = conn.execute("""
        SELECT 
            SUM(CASE WHEN tags LIKE '%DISCOVERY%' THEN 1 ELSE 0 END) as novel,
            COUNT(*) as total
        FROM experiments WHERE created_at > ?
    """, (cutoff,)).fetchone()
    novel_rate = row[0] / max(row[1], 1)
    
    # Domain expansion: fraction of experiments in underrepresented domains (<50 exps total)
    row = conn.execute("""
        SELECT 
            SUM(CASE WHEN domain_count < 50 THEN 1 ELSE 0 END) as expansion,
            COUNT(*) as total
        FROM (
            SELECT e.domain, COUNT(*) OVER (PARTITION BY e.domain) as domain_count
            FROM experiments e WHERE e.created_at > ?
        )
    """, (cutoff,)).fetchone()
    expand_rate = row[0] / max(row[1], 1)
    
    # Domain entropy (diversity)
    row = conn.execute("""
        SELECT domain, COUNT(*) as cnt
        FROM experiments WHERE created_at > ?
        GROUP BY domain
    """, (cutoff,)).fetchall()
    import math
    total = sum(r[1] for r in row)
    entropy = 0
    for r in row:
        p = r[1] / max(total, 1)
        if p > 0:
            entropy -= p * math.log2(p)
    max_entropy = math.log2(max(len(row), 1)) if row else 1
    diversity = entropy / max(max_entropy, 1)
    
    # Transfer rate
    row = conn.execute("""
        SELECT 
            SUM(CASE WHEN tags LIKE '%TRANSFER%' THEN 1 ELSE 0 END) as transfers,
            COUNT(*) as total
        FROM experiments WHERE created_at > ?
    """, (cutoff,)).fetchone()
    transfer_rate = row[0] / max(row[1], 1)
    
    return {
        "confirm_rate": confirm_rate,
        "novel_rate": novel_rate,
        "expand_rate": expand_rate,
        "diversity": diversity,
        "transfer_rate": transfer_rate,
        "window_hours": window_hours,
        "timestamp": time.time(),
    }


def assess_break_rate(conn, window_hours=24):
    """Measure the rate of high-informativeness structural breaks."""
    cutoff = time.time() - (window_hours * 3600)
    try:
        row = conn.execute("""
            SELECT 
                SUM(CASE WHEN break_informativeness >= 10 THEN 1 ELSE 0 END) as high_breaks,
                COUNT(*) as total_validated
            FROM replication_results 
            WHERE replication_status != 'pending' AND validated_at > ?
        """, (cutoff,)).fetchone()
        if row[1] and row[1] > 0:
            return row[0] / row[1]
    except Exception:
        pass
    return 0.0


def compute_allocation(state, prev_weights=None):
    """Compute allocation weights based on current state.
    
    Uses a deficit-proportional allocation:
    - Objectives below their target get MORE weight
    - Objectives above their target get LESS weight
    - Smoothed to prevent oscillation
    """
    # Compute deficit for each objective
    deficits = {}
    for obj, target in TARGETS.items():
        actual = state.get(f"{obj}_rate", 0)
        deficit = max(0, target - actual)  # Only care about under-performance
        surplus = max(0, actual - target)  # Track over-performance
        deficits[obj] = {"deficit": deficit, "surplus": surplus, "actual": actual, "target": target}
    
    # Base weights — now 4 dimensions
    base = {"confirm": 0.35, "novel": 0.25, "expand": 0.25, "break": 0.15}
    
    # Adjust based on deficits
    raw_weights = {}
    for obj in TARGETS:
        d = deficits[obj]["deficit"]
        s = deficits[obj]["surplus"]
        # Boost under-performing, penalize over-performing
        adjustment = (d * 2.0) - (s * 0.5)
        raw_weights[obj] = base[obj] + adjustment
    
    # Normalize to sum to 1.0
    total = sum(raw_weights.values())
    if total > 0:
        for obj in raw_weights:
            raw_weights[obj] /= total
    
    # Clamp
    for obj in raw_weights:
        raw_weights[obj] = max(MIN_WEIGHT, min(MAX_WEIGHT, raw_weights[obj]))
    
    # Re-normalize after clamping
    total = sum(raw_weights.values())
    if total > 0:
        for obj in raw_weights:
            raw_weights[obj] /= total
    
    # Smooth with previous weights (prevent oscillation)
    if prev_weights:
        for obj in raw_weights:
            prev = prev_weights.get(obj, raw_weights[obj])
            delta = raw_weights[obj] - prev
            if abs(delta) > STEP_SIZE:
                raw_weights[obj] = prev + (STEP_SIZE if delta > 0 else -STEP_SIZE)
    
    # Final normalize
    total = sum(raw_weights.values())
    if total > 0:
        for obj in raw_weights:
            raw_weights[obj] /= total
    
    return {
        "weights": raw_weights,
        "deficits": deficits,
        "state": state,
    }


def apply_weights(conn, weights):
    """Update combined_score in curiosities table using new weights."""
    w_confirm = weights["confirm"]
    w_novel = weights["novel"]
    w_expand = weights["expand"]
    
    conn.execute("""
        UPDATE curiosities
        SET combined_score = ?
        WHERE p_confirm IS NOT NULL AND p_novel IS NOT NULL AND p_expand IS NOT NULL
        AND status = 'active'
    """, (None,))  # Placeholder — we need a CASE expression
    
    # Actually compute per-row
    conn.execute("""
        UPDATE curiosities
        SET combined_score = MIN(100, MAX(0,
            CAST(
                ? * p_confirm * 100 +
                ? * p_novel * 100 +
                ? * p_expand * 100
            AS INTEGER)
        ))
        WHERE p_confirm IS NOT NULL AND p_novel IS NOT NULL AND p_expand IS NOT NULL
        AND status = 'active'
    """, (w_confirm, w_novel, w_expand))
    
    updated = conn.total_changes
    conn.commit()
    return updated


def save_state(allocation):
    """Save allocation state for history tracking."""
    history = []
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
                history = data.get("history", [])
        except Exception:
            pass
    
    history.append({
        "timestamp": allocation["state"]["timestamp"],
        "weights": allocation["weights"],
        "state": {k: round(v, 4) for k, v in allocation["state"].items() if isinstance(v, (int, float))},
        "deficits": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in allocation["deficits"].items()},
    })
    
    # Keep last 100 entries
    history = history[-100:]
    
    with open(STATE_PATH, "w") as f:
        json.dump({"history": history, "current": allocation["weights"]}, f, indent=2)


def load_prev_weights():
    """Load previous weights for smoothing."""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
                return data.get("current", None)
        except Exception:
            pass
    return None


def print_allocation(allocation, state, detailed=False):
    """Pretty-print the allocation."""
    weights = allocation["weights"]
    deficits = allocation["deficits"]
    
    print(f"\n{'='*55}")
    print(f"PORTFOLIO ALLOCATION")
    print(f"{'='*55}")
    print(f"Window: {state['window_hours']}h")
    print()
    
    for obj in ["confirm", "novel", "expand", "break"]:
        w = weights.get(obj, 0)
        d = deficits.get(obj, {})
        bar_len = int(w * 40)
        bar = "█" * bar_len + "░" * (40 - bar_len)
        status = "DEFICIT" if d.get("deficit", 0) > 0 else "OK" if d.get("surplus", 0) < 0.1 else "SURPLUS"
        print(f"  P({obj:<8}) {w:.1%}  [{bar}]")
        print(f"    actual={d.get('actual', 0):.1%}  target={d.get('target', 0):.1%}  "
              f"deficit={d.get('deficit', 0):.1%}  surplus={d.get('surplus', 0):.1%}  {status}")
    
    print(f"\n  Diversity:  {state['diversity']:.1%}")
    print(f"  Transfer:   {state.get('transfer_rate', 0):.1%}")
    print(f"{'='*55}")


def main():
    parser = argparse.ArgumentParser(description="Portfolio allocator")
    parser.add_argument("--apply", action="store_true", help="Apply weights to curiosities")
    parser.add_argument("--history", action="store_true", help="Show allocation history")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()
    
    # Flock protection — prevent concurrent runs
    import fcntl
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Another instance running, skipping.")
        lock_fd.close()
        return
    
    try:
        conn = get_db()
        
        if args.history:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH) as f:
                    data = json.load(f)
                history = data.get("history", [])
                print(f"Allocation history ({len(history)} entries):")
                for h in history[-10:]:
                    ts = datetime.fromtimestamp(h["timestamp"]).strftime("%m-%d %H:%M")
                    w = h["weights"]
                    print(f"  {ts}: C={w['confirm']:.2f} N={w['novel']:.2f} E={w['expand']:.2f}")
            else:
                print("No history yet.")
            conn.close()
            return
        
        state = assess_state(conn)
        state["break_rate"] = assess_break_rate(conn)
        prev = load_prev_weights()
        allocation = compute_allocation(state, prev)
        
        if args.apply:
            updated = apply_weights(conn, allocation["weights"])
            save_state(allocation)
            print(f"Applied weights to {updated} curiosities")
            print(f"  confirm={allocation['weights']['confirm']:.3f} "
                  f"novel={allocation['weights']['novel']:.3f} "
                  f"expand={allocation['weights']['expand']:.3f}")
        elif args.json:
            print(json.dumps({
                "weights": allocation["weights"],
                "state": {k: round(v, 4) for k, v in state.items() if isinstance(v, (int, float))},
                "deficits": {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in allocation["deficits"].items()},
            }, indent=2))
            save_state(allocation)
        else:
            print_allocation(allocation, state)
            save_state(allocation)
        
        conn.close()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fd.close()


if __name__ == "__main__":
    main()
