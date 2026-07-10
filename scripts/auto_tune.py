#!/usr/bin/env python3
"""
auto_tune.py — Self-tuning controller for outcome-aware routing.

Reads calibration metrics and adjusts system parameters automatically.
Run every 5-10 minutes. This is the brain of the feedback loop.

Adjusts:
  - outcome_bonus weight in curiosity_scorer.py
  - injection rate in inject_opportunities.py
  - novelty weight based on hidden bridge count
  - trust_weight (in-memory / state file only)

Usage:
    python3 auto_tune.py           # Run tuning cycle
    python3 auto_tune.py --status  # Just print current state
    python3 auto_tune.py --dry-run # Show what would change
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from db_retry import get_db

HERMES = Path(os.path.expanduser("~/.hermes"))
DB_PATH = HERMES / "prometheus.db"
EXPORT_PATH = HERMES / "topology_full_export.json"
OUTCOME_CACHE = HERMES / "routing_outcome_cache.json"
TUNE_STATE = HERMES / "auto_tune_state.json"
SCORER_PATH = HERMES / "scripts" / "curiosity_scorer.py"

# Tuning thresholds — flow r near 0.03 is expected/healthy, not a problem
R_TARGET = 0.05               # Slightly above baseline flow r
R_FLOOR = 0.03                # Below baseline flow r = genuinely degraded
R_CEILING = 2.00              # Raised: flow_r unreliable with few samples and route concentration
HIDDEN_BRIDGE_MIN = 5         # Below this = increase novelty (below baseline = need exploration)
HIDDEN_BRIDGE_TARGET = 15     # Target hidden bridge count (above baseline = healthy)
MIN_RESULTS_FOR_TUNING = 90  # Need this many outcome results before tuning (lowered from 100: 94 samples already collected, sufficient for calibration)
MIN_TIME_BETWEEN_ADJUSTMENTS = 300  # 5 minutes between any parameter change
CEILING_TRAP_COOLDOWN = 1800  # 30 minutes freeze if 3+ ceiling traps in last 5
DEEP_DEADLOCK_COOLDOWN = 600  # 10 minutes lock after DEEP DEADLOCK ESCAPE fires
FLOW_R_MIN_SAMPLES = 30       # Ignore flow_r decisions when below this sample count (lowered: 44 samples is sufficient for rough estimate)

# Parameter bounds
OUTCOME_BONUS_MIN = 10
OUTCOME_BONUS_MAX = 50
INJECTION_RATE_MIN = 3
INJECTION_RATE_MAX = 15
NOVELTY_WEIGHT_MIN = 0.10
NOVELTY_WEIGHT_MAX = 0.50
TRUST_WEIGHT_MIN = 0.01  # Lowered from 0.05 to allow deadlock break trust reduction
TRUST_WEIGHT_MAX = 0.30

# Settled ranges — parameters in these zones are stable, no adjustment needed.
# Prevents perpetual oscillation where bounce-back values land at the edge
# of the adjustment range, causing the tuner to immediately resume nudging.
NOVELTY_SETTLED_LOW = 0.15
NOVELTY_SETTLED_HIGH = 0.25
TRUST_SETTLED_LOW = 0.15
TRUST_SETTLED_HIGH = 0.25

# Extreme deviation thresholds — when hidden bridges are this far from target,
# force parameters out of settled range to drive system back toward equilibrium.
HIDDEN_EXTREME_HIGH = HIDDEN_BRIDGE_TARGET * 3  # 45 — 3x target forces exploration
HIDDEN_EXTREME_LOW = max(HIDDEN_BRIDGE_MIN / 2, 2)  # 2.5 (half of min)

# Crisis threshold — when hidden bridges are 5x+ target, dead zone is suspended.
# At this level, the system is stuck in a saturation loop with all exploration
# parameters at extremes but hidden bridges refusing to decrease. Consolidation
# pressure is needed to break the feedback loop.
HIDDEN_BRIDGE_CRISIS = HIDDEN_BRIDGE_TARGET * 5  # 75 — 5x target = crisis

# Crisis consolidation targets — when in crisis mode, move parameters toward
# these values instead of dead zone. These are mid-range values that allow the
# system to settle into better patterns without over-exploring or over-exploiting.
# Updated 2026-06-15: Previous targets (0.10, 0.05, 3) caused oscillation.
# System settles at novelty=0.30, trust=0.13, injection=7-8. Use these as targets.

def check_manual_override_cooldown(state, now, cooldown_cycles=5):
    """Check if a manual override was recently applied. If so, skip crisis
    overrides to let the manual intervention take effect. This prevents the
    auto-tune from immediately undoing manual changes based on small noise."""
    last_manual_time = state.get('last_manual_override_time', 0)
    if last_manual_time > 0:
        # Count adjustments since manual override
        adjustments = state.get('adjustments', [])
        since_manual = sum(1 for a in adjustments if a['time'] > last_manual_time)
        if since_manual < cooldown_cycles:
            return True  # Still in cooldown - respect manual override
    return False
CRISIS_NOVELTY_TARGET = 0.20   # BREAKTHROUGH value — cron_48 achieved hidden=114, flow_r=0.0339 at novelty=0.20. The 0.30 target caused regression #4 (hidden 114→142).
CRISIS_TRUST_TARGET = 0.05     # LOW trust = spread traffic broadly across domains (breakthrough value)
CRISIS_INJECTION_TARGET = 15   # HIGH injection = flood queue with experiments (breakthrough ceiling)


def any_stalemate_recently_perturbed(state, now, cooldown=360):
    """Check if any stalemate parameter was recently adjusted (within cooldown).
    This prevents deep crisis consolidation from undoing stalemate breaker
    perturbations on the very next run when hidden_history shifts slightly."""
    for key in ("last_stalemate_time_novelty", "last_stalemate_time_injection", "last_stalemate_time_trust"):
        if now - state.get(key, 0) < cooldown:
            return True
    return False


def is_consolidation_stagnant(hidden_history, hidden, min_cycles=3, tolerance=2):
    """Detect consolidation stagnation: hidden bridges completely flat at deep crisis.
    
    Returns True when hidden bridges haven't moved more than plus or minus tolerance for
    min_cycles consecutive observations. This indicates the system is stuck in a
    local minimum where consolidation targets are set but parameters aren't moving
    toward them because dead zones are too wide.
    
    At this point, tightening dead zones forces parameters toward more aggressive
    consolidation values (lower novelty, higher trust) to break the stall."""
    if len(hidden_history) < min_cycles:
        return False
    recent = hidden_history[-min_cycles:]
    return max(recent) - min(recent) <= tolerance and min(recent) >= HIDDEN_BRIDGE_TARGET * 5



def load_tune_state():
    if TUNE_STATE.exists():
        with open(TUNE_STATE) as f:
            return json.load(f)
    return {
        "outcome_bonus_weight": 35,
        "injection_rate": 3,
        "novelty_weight": 0.20,
        "trust_weight": 0.15,
        "last_r": 0.0316,
    "last_outcome_r": 0.0,
        "last_hidden_bridges": 470,
        "hidden_history": [],
        "adjustments": [],
        "last_tune_time": 0,
        "freeze_until": 0,
        "last_stalemate_time": 0,
        "last_stagnant_time_novelty": 0,
        "last_stagnant_time_injection": 0,
        "last_stagnant_time_trust": 0,
    }


def save_tune_state(state):
    with open(TUNE_STATE, "w") as f:
        json.dump(state, f, indent=2)


def compute_calibration():
    """Compute flow-only calibration score."""
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

    # Load flow weights
    if not EXPORT_PATH.exists():
        return 0, 0, 0
    try:
        with open(EXPORT_PATH) as f:
            topo = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return 0, 0, 0
    flow_weights = {}
    for edge in topo.get("edges", []):
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if src and tgt:
            flow_weights[(src, tgt)] = edge.get("weight", 0)

    # Compute correlation
    data_points = []
    for edge, stats in pair_data.items():
        if edge in flow_weights and stats["attempts"] >= 3:
            w = flow_weights[edge] / max(flow_weights.values()) if flow_weights else 0
            sr = stats["successes"] / stats["attempts"]
            data_points.append((w, sr))

    if len(data_points) < 10:
        return 0, 0, len(data_points)

    ws = [d[0] for d in data_points]
    rs = [d[1] for d in data_points]
    n = len(ws)
    mx, my = sum(ws)/n, sum(rs)/n
    cov = sum((a-mx)*(b-my) for a,b in zip(ws,rs))/n
    sx = math.sqrt(sum((a-mx)**2 for a in ws)/n)
    sy = math.sqrt(sum((b-my)**2 for b in rs)/n)
    r = cov/(sx*sy) if sx > 0 and sy > 0 else 0

    return r, len(data_points), 0


def compute_outcome_aware_r():
    """Compute correlation between outcome-aware routing scores and success rates.
    
    This is the meaningful metric — correlates routing_score (which encodes
    outcome signals, trust, novelty) with actual transfer success rates.
    Flow r is structurally low because traffic volume doesn't predict success.
    """
    if not OUTCOME_CACHE.exists():
        return 0, 0
    try:
        with open(OUTCOME_CACHE) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return 0, 0
    
    if not cache:
        return 0, 0
    
    data_points = []
    for edge_key, data in cache.items():
        attempts = data.get("attempts", 0)
        success_rate = data.get("success_rate", 0)
        routing_score = data.get("routing_score", 0)
        if attempts >= 3:
            data_points.append((routing_score, success_rate))
    
    if len(data_points) < 10:
        return 0, len(data_points)
    
    scores = [d[0] for d in data_points]
    rates = [d[1] for d in data_points]
    n = len(scores)
    mx = sum(scores) / n if n > 0 else 0
    my = sum(rates) / n if n > 0 else 0
    
    cov = sum((a - mx) * (b - my) for a, b in zip(scores, rates)) / n
    sx = math.sqrt(sum((a - mx)**2 for a in scores) / n)
    sy = math.sqrt(sum((b - my)**2 for b in rates) / n)
    r = cov / (sx * sy) if sx > 0 and sy > 0 else 0
    
    return r, len(data_points)


def count_hidden_bridges():
    """Count edges with high success but insufficient routing traffic.

    DUAL DEFINITION (2026-06-20 fix):
    1. Fresh hidden bridges: SR>=80%, 3<=attempts<5 — newly proven edges that
       haven't received enough traffic yet (RESPONSIVE: changes quickly with injection).
    2. Proven hidden bridges: SR>=80%, attempts>=5, flow < 5% of max — edges with
       extensive confirming data but near-zero actual traffic (the MOST VALUABLE
       undiscovered routes; changes more slowly as flow builds up).

    Previous definition (SR>=80%, 3<=attempts<5) counted only 13 hidden bridges,
    blind to the 25+ proven hidden bridges with 5-44 attempts and zero flow that
    the calibration test detects. This caused the auto-tune to report "healthy"
    (hidden=13 <= 15 target) when the real count was 25-86.

    The combined count is responsive because fresh bridges move quickly, while
    proven bridges require sustained traffic to clear, giving the auto-tune a
    realistic picture of undiscovered route pressure.
    """
    if not OUTCOME_CACHE.exists():
        return 0
    try:
        with open(OUTCOME_CACHE) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return 0
    
    if not cache:
        return 0
    
    # Compute max flow for hidden bridge threshold (matching calibration_test.py)
    max_flow = max(
        (d.get("flow_norm", 0) for d in cache.values() if isinstance(d, dict)),
        default=1.0
    )
    HIDDEN_FLOW_PCT = 0.05  # edges with <5% of max flow are hidden bridges
    
    hidden = 0
    for data in cache.values():
        sr = data.get("success_rate", 0)
        attempts = data.get("attempts", 0)
        flow_norm = data.get("flow_norm", 0)
        
        # Only count proven edges (attempts >= 3) with high success rate
        if sr < 0.80 or attempts < 3:
            continue
        
        # Hidden bridge: high success, proven, but under-routed
        # Definition 1: Fresh hidden bridge (3-4 attempts, barely explored)
        # Definition 2: Proven hidden bridge (5+ attempts, zero/near-zero flow)
        if (3 <= attempts < 5) or (attempts >= 5 and flow_norm / max_flow < HIDDEN_FLOW_PCT):
            hidden += 1
    
    return hidden


def check_routing_collapse():
    """Check if routing has completely collapsed (all edges have zero flow).
    
    When consolidation pushes parameters too low, routing stops entirely.
    With zero flow, hidden bridges can never decrease because work is never
    routed through them. This creates a deadlock that requires explicit
    detection and parameter recovery.
    
    Returns (is_collapsed: bool, total_edges: int, zero_flow_edges: int)
    """
    if not OUTCOME_CACHE.exists():
        return False, 0, 0
    try:
        with open(OUTCOME_CACHE) as f:
            cache = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return False, 0, 0
    
    if not cache:
        return False, 0, 0
    
    total = len(cache)
    zero_flow = sum(1 for d in cache.values() if d.get("flow_norm", 0) <= 0.001)
    
    # Routing is collapsed if >95% of edges have zero flow and there are enough edges
    is_collapsed = total > 50 and zero_flow >= total * 0.95
    return is_collapsed, total, zero_flow


def adjust_scorer_outcome_bonus(new_weight):
    """Adjust the outcome_bonus max in curiosity_scorer.py."""
    if not SCORER_PATH.exists():
        return False
    
    content = SCORER_PATH.read_text()
    # Find the outcome_bonus scaling line
    old_pattern = r"outcome_bonus = int\(_best_score \* \d+\)"
    new_value = f"outcome_bonus = int(_best_score * {new_weight})"
    
    if re.search(old_pattern, content):
        content = re.sub(old_pattern, new_value, content)
        SCORER_PATH.write_text(content)
        return True
    return False


def adjust_injection_rate(new_rate):
    """Adjust the injection count in inject_opportunities.py."""
    inject_path = HERMES / "scripts" / "inject_opportunities.py"
    if not inject_path.exists():
        return False
    
    content = inject_path.read_text()
    old_pattern = r'count = \d+'
    new_value = f'count = {new_rate}'
    
    if re.search(old_pattern, content):
        content = re.sub(old_pattern, new_value, content, count=1)
        inject_path.write_text(content)
        return True
    return False


def detect_ceiling_trap_pattern(adjustments):
    """Check last 5 adjustments for ceiling trap patterns.
    Returns True if 3+ ceiling traps detected — indicates oscillation."""
    recent = adjustments[-5:]
    ceiling_count = sum(1 for a in recent if "ceiling trap" in a.get("adj", "").lower())
    return ceiling_count >= 3


def _tune_outcome_bonus(ctx, args):
    """Phase 1 — outcome_bonus."""
    state = ctx.state
    adjustments = ctx.adjustments
    r = ctx.r
    outcome_r = ctx.outcome_r
    hidden = ctx.hidden
    is_frozen = ctx.is_frozen
    ceiling_trap_detected = ctx.ceiling_trap_detected

    # 1. Tune outcome_bonus based on outcome_r (PRIMARY — always runs)
    # CRISIS HARD CAP: When hidden bridges are 5x+ target, cap outcome_bonus at 40.
    if not is_frozen and hidden >= HIDDEN_BRIDGE_CRISIS and state["outcome_bonus_weight"] > 40:
        new_weight = 40
        adjustments.append(f"CRISIS HARD CAP: outcome_bonus {state['outcome_bonus_weight']} → {new_weight} (hidden={hidden} {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target)")
        if not args.dry_run:
            state["outcome_bonus_weight"] = new_weight
            adjust_scorer_outcome_bonus(new_weight)
    if is_frozen:
        adjustments.append(f"Outcome_bonus frozen at {state['outcome_bonus_weight']} (oscillation guard)")
    elif outcome_r < 0.7:
        # Outcome-aware r is weak — routing scores aren't predicting success well
        # Increase outcome_bonus to give outcome signals more weight
        new_weight = min(state["outcome_bonus_weight"] + 3, OUTCOME_BONUS_MAX)
        if new_weight != state["outcome_bonus_weight"]:
            if ceiling_trap_detected:
                adjustments.append(f"Oscillation guard: skipping outcome_bonus increase (ceiling trap pattern)")
            else:
                adjustments.append(f"Outcome_r low ({outcome_r:.3f} < 0.7) — increasing outcome_bonus: {state['outcome_bonus_weight']} → {new_weight}")
                if not args.dry_run:
                    state["outcome_bonus_weight"] = new_weight
                    adjust_scorer_outcome_bonus(new_weight)
    elif outcome_r > 0.95:
        # Outcome-aware r is strong — routing scores are predicting well
        if hidden >= HIDDEN_BRIDGE_CRISIS:
            if state["outcome_bonus_weight"] > 25:
                new_weight = max(state["outcome_bonus_weight"] - 2, 40)
                adjustments.append(f"Outcome_r strong ({outcome_r:.3f}), crisis — reducing toward breakthrough: {state['outcome_bonus_weight']} → {new_weight}")
                if not args.dry_run:
                    state["outcome_bonus_weight"] = new_weight
                    adjust_scorer_outcome_bonus(new_weight)
            else:
                adjustments.append(f"Outcome_r strong ({outcome_r:.3f}) but hidden={hidden} {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target — keeping at {state['outcome_bonus_weight']}")
        else:
            new_weight = max(state["outcome_bonus_weight"] - 2, OUTCOME_BONUS_MIN)
            if new_weight != state["outcome_bonus_weight"]:
                adjustments.append(f"Outcome_r strong ({outcome_r:.3f} > 0.95) — decreasing outcome_bonus: {state['outcome_bonus_weight']} → {new_weight}")
                if not args.dry_run:
                    state["outcome_bonus_weight"] = new_weight
                    adjust_scorer_outcome_bonus(new_weight)
    else:
        adjustments.append(f"Outcome_r={outcome_r:.3f} in healthy range (0.7-0.95), no outcome_bonus change")

    # 1b. Secondary flow_r signal — only for crisis/injection decisions
    # Flow r is structurally near zero (traffic volume doesn't predict success).
    # Only use it for extreme cases where outcome_r alone isn't enough.
    if r < 0 and outcome_r < 0.5:
        # Both signals negative — system is genuinely struggling
        pulled = False
        if state["injection_rate"] > INJECTION_RATE_MIN + 2:
            new_rate = max(state["injection_rate"] - 3, INJECTION_RATE_MIN)
            adjustments.append(f"Both r negative (flow={r:.4f}, outcome={outcome_r:.3f}) — decreasing injection: {state['injection_rate']} → {new_rate}")
            if not args.dry_run:
                state["injection_rate"] = new_rate
                adjust_injection_rate(new_rate)
                pulled = True
        if state["novelty_weight"] > NOVELTY_WEIGHT_MIN + 0.10:
            new_novelty = max(state["novelty_weight"] - 0.10, NOVELTY_WEIGHT_MIN)
            adjustments.append(f"Both r negative — decreasing novelty: {state['novelty_weight']:.2f} → {new_novelty:.2f}")
            if not args.dry_run:
                state["novelty_weight"] = new_novelty
                pulled = True
        if not pulled and state["outcome_bonus_weight"] > OUTCOME_BONUS_MIN:
            new_weight = max(state["outcome_bonus_weight"] - 5, OUTCOME_BONUS_MIN)
            adjustments.append(f"Both r negative — decreasing outcome_bonus: {state['outcome_bonus_weight']} → {new_weight}")
            if not args.dry_run:
                state["outcome_bonus_weight"] = new_weight
                adjust_scorer_outcome_bonus(new_weight)

    # 1b. Outcome_r-based outcome_bonus adjustment (always runs, regardless of flow_r)
    # This ensures the system responds to poor routing quality even when flow_r is "fine"
    if not is_frozen:
        if outcome_r < 0.7 and state["outcome_bonus_weight"] < OUTCOME_BONUS_MAX:
            new_weight = min(state["outcome_bonus_weight"] + 3, OUTCOME_BONUS_MAX)
            if new_weight != state["outcome_bonus_weight"]:
                if ceiling_trap_detected:
                    adjustments.append(f"Oscillation guard: skipping outcome_bonus increase (ceiling trap pattern)")
                else:
                    adjustments.append(f"Outcome_r low ({outcome_r:.3f} < 0.7) — increasing outcome_bonus: {state['outcome_bonus_weight']} → {new_weight}")
                    if not args.dry_run:
                        state["outcome_bonus_weight"] = new_weight
                        adjust_scorer_outcome_bonus(new_weight)
        elif outcome_r > 0.95 and state["outcome_bonus_weight"] > OUTCOME_BONUS_MIN:
            # During crisis: force outcome_bonus DOWN toward 25 (breakthrough value).
            # Threshold raised from 0.85 to 0.95 to prevent auto-tune from immediately
            # undoing manual deadlock break interventions (outcome_r=0.85 was triggering).
            if hidden >= HIDDEN_BRIDGE_CRISIS:
                if state["outcome_bonus_weight"] > 25:
                    new_weight = max(state["outcome_bonus_weight"] - 2, 40)
                    adjustments.append(f"Outcome_r strong ({outcome_r:.3f}), crisis — reducing toward breakthrough: {state['outcome_bonus_weight']} → {new_weight}")
                    if not args.dry_run:
                        state["outcome_bonus_weight"] = new_weight
                        adjust_scorer_outcome_bonus(new_weight)
                else:
                    adjustments.append(f"Outcome_r strong but hidden={hidden} {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target — keeping outcome_bonus at {state['outcome_bonus_weight']} (at breakthrough)")
            else:
                new_weight = max(state["outcome_bonus_weight"] - 2, 30)
                if new_weight != state["outcome_bonus_weight"]:
                    adjustments.append(f"Outcome_r strong ({outcome_r:.3f} > 0.95) — decreasing outcome_bonus: {state['outcome_bonus_weight']} → {new_weight}")
                    if not args.dry_run:
                        state["outcome_bonus_weight"] = new_weight
                        adjust_scorer_outcome_bonus(new_weight)


def _tune_novelty(ctx, args):
    """Phase 2 — novelty. Publishes to ctx: novelty, trust, current_inject,
    extreme_high, hidden_growing, hidden_history, normal_crisis_stagnant."""
    state = ctx.state
    adjustments = ctx.adjustments
    r = ctx.r
    hidden = ctx.hidden
    routing_collapsed = ctx.routing_collapsed
    total_edges = ctx.total_edges
    zero_flow_edges = ctx.zero_flow_edges
    now = ctx.now

    # 2. Tune novelty based on hidden bridges
    # Read injection rate early — needed by crisis/stagnant logic below
    current_inject = state.get("injection_rate", 5)
    trust = state.get("trust_weight", 0.15)  # Read early — needed by deadlock breaker
    novelty = state["novelty_weight"]  # Read early — used in all tuning branches below
    extreme_high = hidden >= HIDDEN_EXTREME_HIGH  # 3x+ target
    # Track trend using moving window (last N values) to avoid flip-flop
    # where one run sees growth (161 > 159) and next sees no growth (161 == 161)
    # History contains PREVIOUS observations only — current value is compared
    # against the history but not added until after this run completes.
    hidden_history = state.get("hidden_history", [])
    # Keep only last 5 previous values
    if len(hidden_history) > 5:
        hidden_history = hidden_history[-5:]
    
    # Growing if current > average of RECENT previous values (trailing window).
    # Uses last 3 values to avoid stale-history false positives. A one-time jump
    # followed by stabilization should NOT be classified as "growing".
    if len(hidden_history) >= 3:
        recent_vals = hidden_history[-3:]  # Only the most recent 3 values
        recent_avg = sum(recent_vals) / 3
        hidden_growing = hidden > recent_avg * 1.03  # 3% threshold for trend detection
    elif len(hidden_history) >= 2:
        hidden_growing = hidden > hidden_history[-1]
    elif len(hidden_history) == 1:
        hidden_growing = hidden > hidden_history[0]
    else:
        hidden_growing = hidden > state.get("last_hidden_bridges", 0)

    # Default: no stagnation detected. Set to True in novelty section when
    # hidden bridges flat at crisis level for 5+ cycles. Used by injection
    # section to avoid reversing deadlock breaker consolidation.
    normal_crisis_stagnant = False

    if hidden < HIDDEN_BRIDGE_MIN:
        # Too few hidden bridges — increase exploration
        # NOTE: hidden == 0 is ALWAYS critical regardless of outcome_r.
        # High outcome_r only means existing routes perform well — it does NOT
        # mean new cross-domain connections are being discovered.
        if hidden == 0:
            # Zero hidden bridges: discovery could be stalled, but with
            # SUCCESS_FLOOR, hidden=0 is often the correct state.
            # When flow r is negative, REDUCE novelty to stop pushing toward
            # unexplored edges that don't succeed.
            novelty = state["novelty_weight"]
            if r < 0 and novelty > 0.15:
                # Flow r negative: over-exploration. Pull back novelty to floor 0.15.
                new_novelty = max(novelty - 0.05, 0.15)
                adjustments.append(f"Decrease novelty: {novelty:.2f} → {new_novelty:.2f} (hidden=0, flow r={r:.4f} < 0, over-exploration)")
                if not args.dry_run:
                    state["novelty_weight"] = new_novelty
            elif r < 0 and novelty <= 0.15:
                # Flow r negative but novelty already at floor — HOLD.
                # Without this guard, the elif below fires and pushes novelty
                # back up, re-creating the over-exploration it just corrected.
                adjustments.append(f"Novelty HOLD at floor: {novelty:.2f} (hidden=0, flow r={r:.4f} < 0, novelty floor 0.15 reached)")
            elif routing_collapsed and novelty > 0.15:
                # ROUTING COLLAPSE: when 95%+ edges have zero flow, high novelty
                # is flooding routing with unexplored edges. DECREASE novelty to
                # allow proven routes to dominate and restore routing flow.
                new_novelty = max(novelty - 0.05, 0.15)
                adjustments.append(f"ROUTING COLLAPSE (hidden=0): novelty {novelty:.2f} → {new_novelty:.2f} ({zero_flow_edges}/{total_edges} edges zero flow, reducing exploration to restore routing)")
                if not args.dry_run:
                    state["novelty_weight"] = new_novelty
            elif novelty < NOVELTY_WEIGHT_MAX:
                new_novelty = min(novelty + 0.05, NOVELTY_WEIGHT_MAX)
                adjustments.append(f"Increase novelty: {novelty:.2f} → {new_novelty:.2f} (hidden=0, discovery stalled)")
                if not args.dry_run:
                    state["novelty_weight"] = new_novelty
            else:
                adjustments.append(f"Novelty at max: {novelty:.2f} (hidden=0, no room to increase)")
        else:
            if routing_collapsed and state["novelty_weight"] > 0.15:
                # ROUTING COLLAPSE: when 95%+ edges have zero flow, reduce novelty
                # to allow proven routes to dominate and restore routing flow.
                new_novelty = max(state["novelty_weight"] - 0.05, 0.15)
                adjustments.append(f"ROUTING COLLAPSE (hidden={hidden}): novelty {state['novelty_weight']:.2f} → {new_novelty:.2f} ({zero_flow_edges}/{total_edges} edges zero flow, reducing exploration to restore routing)")
                if not args.dry_run:
                    state["novelty_weight"] = new_novelty
            else:
                new_novelty = min(state["novelty_weight"] + 0.05, NOVELTY_WEIGHT_MAX)
                if new_novelty != state["novelty_weight"]:
                    adjustments.append(f"Increase novelty: {state['novelty_weight']:.2f} → {new_novelty:.2f}")
                    if not args.dry_run:
                        state["novelty_weight"] = new_novelty
    elif extreme_high:
        # Extreme high hidden bridges (3x+ target)
        # TREND-AWARE: When hidden bridges are extreme AND growing, the system needs
        # MORE exploration to surface hidden routes, not consolidation. Dead zone
        # only applies when hidden bridges are stable or declining (consolidation working).
        # DEAD ZONE: 0.20-0.35 is the equilibrium band for extreme_high (stable only).
        NOVELTY_EXTREME_LOW = 0.20
        NOVELTY_EXTREME_HIGH = 0.35
        crisis = hidden >= HIDDEN_BRIDGE_CRISIS
        if crisis and hidden_growing:
            # CRISIS + GROWING: hidden bridges at 5x+ target AND still increasing.
            # But at 10x+ target (deep crisis), all exploration params are at extremes
            # and pushing harder creates a saturation loop. Consolidate to break it.
            novelty = state["novelty_weight"]
            crisis_ratio = hidden / HIDDEN_BRIDGE_TARGET
            if crisis_ratio >= 10:
                # DEEP CRISIS + GROWING: consolidate toward mid-range targets.
                # All params at extremes means exploration can't go higher.
                # Consolidation breaks the saturation feedback loop.
                CRISIS_EXPLORE_CAP = 0.45
                if novelty > CRISIS_NOVELTY_TARGET:
                    if r < 0:
                        # FLOW DEAD: routing killed — HOLD instead of consolidating down
                        adjustments.append(f"DEEP CRISIS GROWING HOLD (flow): novelty {novelty:.2f} (r={r:.4f} < 0, hidden={hidden} {crisis_ratio:.1f}x target, holding to preserve routing)")
                    else:
                        new_novelty = max(novelty - 0.03, CRISIS_NOVELTY_TARGET)
                        adjustments.append(f"DEEP CRISIS CONSOLIDATION (growing): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, breaking saturation)")
                        if not args.dry_run:
                            state["novelty_weight"] = new_novelty
                elif novelty < CRISIS_NOVELTY_TARGET:
                    new_novelty = min(novelty + 0.03, CRISIS_NOVELTY_TARGET)
                    adjustments.append(f"DEEP CRISIS PUSH (growing): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, pushing toward consolidation)")
                    if not args.dry_run:
                        state["novelty_weight"] = new_novelty
                else:
                    adjustments.append(f"Novelty at consolidation target: {novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, deep crisis)")
            else:
                # NORMAL CRISIS + GROWING: push exploration harder to surface bridges
                CRISIS_EXPLORE_CAP = 0.45  # Aggressive but not maxed
                if novelty < CRISIS_EXPLORE_CAP:
                    new_novelty = min(novelty + 0.03, CRISIS_EXPLORE_CAP)
                    adjustments.append(f"CRISIS EXPLORATION PUSH (growing): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, need more exploration)")
                    if not args.dry_run:
                        state["novelty_weight"] = new_novelty
                else:
                    adjustments.append(f"Novelty at crisis explore cap: {novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, growing)")
        elif crisis:
            # CRISIS + STABLE: hidden bridges 5x+ target but not growing.
            # Only consolidate if parameters are at exploration extremes (saturation loop).
            # If parameters are at mid-range, the system still needs MORE exploration.
            novelty = state["novelty_weight"]
            NOVELTY_AT_EXTREME = 0.40  # Only consolidate if novelty is at exploration extreme
            # Dead zone width scales inversely with crisis severity:
            # - 5x target (75): narrow dead zone [0.15, 0.35] (force consolidation)
            # - 10x target (150): very narrow [0.25, 0.30] (hard consolidation)
            # - 15x+ target (225+): bypass dead zone entirely (extreme crisis)
            crisis_ratio = hidden / HIDDEN_BRIDGE_TARGET
            if crisis_ratio >= 15:
                # EXTREME CRISIS: bypass dead zone, consolidate HARD toward target
                # STALEMATE BREAKER: DISABLED at extreme crisis (>=15x target)
                # System needs hard consolidation, not exploration perturbation.
                # STAGNANT DETECTION: If hidden bridges flat for 5+ cycles at target,
                # break dead zone with small perturbation. Without this, the system
                # gets stuck when all params land exactly at consolidation targets
                # but hidden bridges don't decline.
                extreme_stagnant = False
                if len(hidden_history) >= 5:
                    recent5 = hidden_history[-5:]
                    # Use percentage-based tolerance: if range is < 1.5% of value, it's stagnant
                    h_range = max(recent5) - min(recent5)
                    h_min = min(recent5)
                    h_pct_range = h_range / h_min if h_min > 0 else 999
                    if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15:
                        extreme_stagnant = True

                STAGNANT_COOLDOWN = 120  # 2 minutes (reduced for crisis responsiveness)
                last_stagnant_novelty = state.get("last_stagnant_time_novelty", 0)
                stagnant_novelty_cooldown = now - last_stagnant_novelty < STAGNANT_COOLDOWN

                # Stagnant targets aligned with crisis consolidation targets
                STAGNANT_EXTREME_NOVELTY = 0.15  # Aggressive consolidation floor
                STAGNANT_EXTREME_INJECT = 10  # High injection to flood queue
                STAGNANT_EXTREME_TRUST = 0.15  # Higher trust to penalize underconfident domains
                if extreme_stagnant and not stagnant_novelty_cooldown:
                    # STAGNANT AT EXTREME CRISIS: hidden bridges flat at 15x+ target.
                    # Parameters at consolidation targets aren't working — system frozen.
                    # Small perturbation to break deadlock. NOT full stalemate breaker.
                    # FIX: When flow r is critically low (negative or near zero), pushing
                    # params toward minimums makes routing worse, not better. HOLD instead.
                    if novelty < STAGNANT_EXTREME_NOVELTY:
                        new_novelty = min(novelty + 0.02, STAGNANT_EXTREME_NOVELTY)
                        adjustments.append(f"EXTREME STAGNANT PERTURB: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, breaking dead zone)")
                        if not args.dry_run:
                            state["novelty_weight"] = new_novelty
                            state["last_stagnant_time_novelty"] = now
                    elif novelty > STAGNANT_EXTREME_NOVELTY:
                        # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                        escape_threshold_novelty_nonbypass = -0.05 if crisis_ratio >= 5 else 0
                        if r < escape_threshold_novelty_nonbypass:
                            # Flow deeply negative — consolidation has killed routing.
                            # Push UP toward exploration to escape consolidation trap.
                            # Hidden bridges are stagnant, consolidation isn't working.
                            new_novelty = min(novelty + 0.02, 0.15)
                            adjustments.append(f"CONSOLIDATION TRAP ESCAPE: novelty {novelty:.2f} → {new_novelty:.2f} (flow r={r:.4f} < 0, pushing toward exploration to restore routing)")
                            if not args.dry_run:
                                state["novelty_weight"] = new_novelty
                                state["last_stagnant_time_novelty"] = now
                        elif r < 0.01:
                            # Flow critically low but not negative.
                            # CRISIS OVERRIDE: When hidden bridges flat at 18x+ target,
                            # consolidation is MORE important than routing capacity.
                            # Push DOWN toward consolidation target instead of holding.
                            # STAGNANT CRISIS ESCAPE: If hidden bridges have been flat for
                            # 5+ cycles, consolidation has FAILED. Push UP to try exploration.
                            if crisis_ratio >= 18 and novelty > STAGNANT_EXTREME_NOVELTY + 0.02:
                                # Check if consolidation has been stuck (hidden flat 5+ cycles)
                                consolidation_stuck = False
                                if len(hidden_history) >= 5:
                                    recent5 = hidden_history[-5:]
                                    h_range = max(recent5) - min(recent5)
                                    h_min = min(recent5)
                                    h_pct_range = h_range / h_min if h_min > 0 else 999
                                    if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15:
                                        consolidation_stuck = True
                                if consolidation_stuck:
                                    # CONSOLIDATION FAILED: hidden bridges flat despite params at targets.
                                    # Push UP to try exploration — different approach to break local minimum.
                                    # FIX 2026-06-15: Cap was 0.18 → no-op. Raised to 0.30 → still no-op at current=0.30.
                                    # FIX 2026-06-15-v2: Cap now 0.40 (80% of max 0.50) to allow genuine increase.

                                    # OSCILLATION DETECTION: If last adjustment was a DEADLOCK BREAKER (DOWN),
                                    # and now we're trying to push UP, we're in a consolidation↔exploration loop.
                                    # After 2 full oscillation cycles, HOLD instead of oscillating further.
                                    osc_count = state.get("oscillation_count", 0)
                                    last_dir = state.get("last_novelty_direction", None)
                                    if last_dir == "down":
                                        osc_count += 1
                                    else:
                                        osc_count = 0  # reset if pattern broken

                                    if osc_count >= 2:
                                        # OSCILLATION BREAKER: 2 full cycles of up→down detected.
                                        # Hold novelty steady, focus on injection to flood queue.
                                        adjustments.append(f"OSCILLATION BREAKER: novelty {novelty:.2f} HELD (detected {osc_count} consolidation↔exploration cycles, holding to break loop)")
                                        if not args.dry_run:
                                            state["oscillation_count"] = 0
                                            state["last_novelty_direction"] = "hold"
                                    else:
                                        new_novelty = min(novelty + 0.05, 0.40)
                                        if new_novelty <= novelty:
                                            # DEADLOCK BREAKER: Exploration can't go higher (already at cap).
                                            # Both consolidation AND exploration have failed.
                                            # Flip to hard consolidation — force novelty DOWN toward crisis target.
                                            new_novelty = max(novelty - 0.10, CRISIS_NOVELTY_TARGET)
                                            adjustments.append(f"DEADLOCK BREAKER: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, exploration maxed & consolidation failed, forcing hard consolidation)")
                                            if not args.dry_run:
                                                state["novelty_weight"] = new_novelty
                                                state["last_novelty_direction"] = "down"
                                                state["oscillation_count"] = osc_count
                                        else:
                                            adjustments.append(f"STAGNANT CRISIS ESCAPE (consolidation failed): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat for 5+ cycles, consolidation not working, trying exploration)")
                                            if not args.dry_run:
                                                state["novelty_weight"] = new_novelty
                                                state["last_novelty_direction"] = "up"
                                                state["oscillation_count"] = osc_count
                                    if not args.dry_run:
                                        state["last_stagnant_time_novelty"] = now
                                else:
                                    new_novelty = max(novelty - 0.02, STAGNANT_EXTREME_NOVELTY)
                                    adjustments.append(f"EXTREME CRISIS CONSOLIDATION (flow override): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat, consolidating despite low flow)")
                                    if not args.dry_run:
                                        state["novelty_weight"] = new_novelty
                                        state["last_stagnant_time_novelty"] = now
                            elif novelty <= STAGNANT_EXTREME_NOVELTY + 0.08:
                                # CRISIS GUARD: At 18x+ target, consolidation overrides routing
                                if crisis_ratio >= 18:
                                    # ROUTING COLLAPSE ESCAPE: When ALL flow is zero, consolidation
                                    # has killed routing entirely. Push params UP to restore routing.
                                    if routing_collapsed:
                                        new_novelty = min(novelty + 0.04, 0.18)
                                        adjustments.append(f"ROUTING COLLAPSE ESCAPE: novelty {novelty:.2f} → {new_novelty:.2f} (routing dead — {zero_flow_edges}/{total_edges} edges zero flow, restoring routing to break deadlock)")
                                        if not args.dry_run:
                                            state["novelty_weight"] = new_novelty
                                            state["last_stagnant_time_novelty"] = now
                                    else:
                                        # DEADLOCK FIX: At stagnant target with flat hidden bridges,
                                        # consolidation has FAILED. Force exploration to break local minimum.
                                        # Hidden bridges can't decrease without routing traffic through them.
                                        if extreme_stagnant:
                                            new_novelty = min(novelty + 0.05, 0.40)
                                            if new_novelty <= novelty:
                                                # Both consolidation AND exploration failed at this param.
                                                # Hold and let other params (injection, trust) try to break out.
                                                adjustments.append(f"NOVELTY DEADLOCK: novelty {novelty:.2f} (at stagnant target, exploration maxed, holding)")
                                            else:
                                                adjustments.append(f"STAGNANT TARGET ESCAPE (consolidation failed): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat for 5+ cycles, at floor — forcing exploration)")
                                                if not args.dry_run:
                                                    state["novelty_weight"] = new_novelty
                                                    state["last_stagnant_time_novelty"] = now
                                        else:
                                            adjustments.append(f"EXTREME CRISIS HOLD (consolidation priority): novelty {novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, consolidation overrides routing)")
                                else:
                                    new_novelty = min(novelty + 0.03, 0.18)
                                    adjustments.append(f"EXTREME STAGNANT MIN-ROUTING (near target): novelty {novelty:.2f} → {new_novelty:.2f} (flow r={r:.4f} < 0.01, restoring minimal routing)")
                                    if not args.dry_run:
                                        state["novelty_weight"] = new_novelty
                                        state["last_stagnant_time_novelty"] = now
                            else:
                                adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): novelty {novelty:.2f} (flow r={r:.4f} < 0.005, holding above target to preserve routing)")
                        else:
                            # Only PERTURB DOWN when flow r is healthy enough.
                            # At r < 0.01, system still needs more routing capacity.
                            if r >= 0.01:
                                new_novelty = max(novelty - 0.02, STAGNANT_EXTREME_NOVELTY)
                                adjustments.append(f"EXTREME STAGNANT PERTURB: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, breaking dead zone)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                                    state["last_stagnant_time_novelty"] = now
                            else:
                                adjustments.append(f"EXTREME STAGNANT HOLD (flow low): novelty {novelty:.2f} (flow r={r:.4f} < 0.01, preserving routing capacity)")
                    else:
                        # At stagnant target — check for minimum viable routing
                        # When flow r is critically low, system is paralyzed. Push novelty
                        # slightly above stagnant target to restore minimal routing capacity.
                        if r < 0.01:
                            # CRISIS GUARD: At 18x+ target, consolidation overrides routing
                            if crisis_ratio >= 18:
                                # ROUTING COLLAPSE ESCAPE: When ALL flow is zero, consolidation
                                # has killed routing entirely. Hidden bridges can never decrease
                                # without flow. Push params UP to restore minimal routing.
                                if routing_collapsed:
                                    new_novelty = min(novelty + 0.04, 0.18)
                                    adjustments.append(f"ROUTING COLLAPSE ESCAPE: novelty {novelty:.2f} → {new_novelty:.2f} (routing dead — {zero_flow_edges}/{total_edges} edges zero flow, restoring routing to break deadlock)")
                                    if not args.dry_run:
                                        state["novelty_weight"] = new_novelty
                                        state["last_stagnant_time_novelty"] = now
                                elif extreme_stagnant:
                                    # DEADLOCK FIX: At stagnant target with flat hidden bridges,
                                    # consolidation has FAILED. Force exploration to break local minimum.
                                    new_novelty = min(novelty + 0.05, 0.40)
                                    if new_novelty <= novelty:
                                        adjustments.append(f"NOVELTY DEADLOCK: novelty {novelty:.2f} (at stagnant target, exploration maxed, holding)")
                                    else:
                                        adjustments.append(f"STAGNANT TARGET ESCAPE (consolidation failed): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat for 5+ cycles, at floor — forcing exploration)")
                                        if not args.dry_run:
                                            state["novelty_weight"] = new_novelty
                                            state["last_stagnant_time_novelty"] = now
                                else:
                                    adjustments.append(f"EXTREME CRISIS HOLD (consolidation priority): novelty {novelty:.2f} (at stagnant target, consolidation overrides routing)")
                            else:
                                new_novelty = min(novelty + 0.03, 0.18)
                                adjustments.append(f"EXTREME STAGNANT MIN-ROUTING: novelty {novelty:.2f} → {new_novelty:.2f} (flow r={r:.4f} < 0.01, restoring minimal routing)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                                    state["last_stagnant_time_novelty"] = now
                        else:
                            # HOLD (don't push further, prevents bounce-back oscillation)
                            adjustments.append(f"EXTREME STAGNANT HOLD: novelty {novelty:.2f} (at stagnant target, holding)")
                elif extreme_stagnant and stagnant_novelty_cooldown:
                    # Crisis bypass: when flow r critically low, bypass stagnant cooldown
                    if r < 0.01:
                        adjustments.append(f"CRISIS BYPASS: novelty stagnant cooldown bypassed (flow r={r:.4f} < 0.01, extreme stagnant)")
                        # FIX 2026-06-15: Trap escape was triggering every cycle at flow r=-0.008
                        # (barely negative, within normal variance), preventing crisis consolidation
                        # from persisting long enough to reduce hidden bridges. Added crisis-mode guard:
                        # when hidden bridges >= 5x target, require r < -0.05 (truly negative) before escape.
                        if novelty > STAGNANT_EXTREME_NOVELTY:
                            # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                            escape_threshold = -0.05 if crisis_ratio >= 5 else 0
                            if r < escape_threshold:
                                # Flow deeply negative — consolidation trap. Push UP.
                                new_novelty = min(novelty + 0.02, 0.18)
                                adjustments.append(f"CONSOLIDATION TRAP ESCAPE (bypass): novelty {novelty:.2f} → {new_novelty:.2f} (flow r={r:.4f} < 0, pushing toward exploration)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                                    state["last_stagnant_time_novelty"] = now
                            elif routing_collapsed:
                                # ROUTING COLLAPSE: all flow zero — push UP to restore routing
                                new_novelty = min(novelty + 0.04, 0.18)
                                adjustments.append(f"ROUTING COLLAPSE ESCAPE (bypass): novelty {novelty:.2f} → {new_novelty:.2f} (routing dead, restoring routing to break deadlock)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                                    state["last_stagnant_time_novelty"] = now
                            else:
                                # DEADLOCK BREAKER (bypass): params far above consolidation targets
                                # but no trap escape or routing collapse. Force consolidation.
                                if hidden_history and len(hidden_history) >= 5:
                                    recent5 = hidden_history[-5:]
                                    h_range = max(recent5) - min(recent5)
                                    h_min = min(recent5)
                                    h_pct_range = h_range / h_min if h_min > 0 else 999
                                    if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15 and novelty >= STAGNANT_EXTREME_NOVELTY + 0.10:
                                        new_novelty = max(novelty - 0.05, STAGNANT_EXTREME_NOVELTY)
                                        adjustments.append(f"DEADLOCK BREAKER (bypass): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat 5+ cycles, params far above target, forcing consolidation)")
                                        if not args.dry_run:
                                            state["novelty_weight"] = new_novelty
                                            state["last_stagnant_time_novelty"] = now
                                            state["last_novelty_direction"] = "down"
                                            state["oscillation_count"] = state.get("oscillation_count", 0)
                                    else:
                                        adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): novelty {novelty:.2f} (flow r critically low, holding above target to preserve routing)")
                                else:
                                    adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): novelty {novelty:.2f} (flow r critically low, holding above target to preserve routing)")
                        elif novelty < STAGNANT_EXTREME_NOVELTY:
                            new_novelty = min(novelty + 0.03, STAGNANT_EXTREME_NOVELTY)
                            adjustments.append(f"EXTREME STAGNANT PERTURB (crisis): novelty {novelty:.2f} → {new_novelty:.2f} (flow r critically low, bypassing cooldown)")
                            if not args.dry_run:
                                state["novelty_weight"] = new_novelty
                                state["last_stagnant_time_novelty"] = now
                        else:
                            adjustments.append(f"EXTREME STAGNANT HOLD: novelty {novelty:.2f} (at stagnant target, holding)")
                    else:
                        adjustments.append(f"Stagnant perturbation cooldown (novelty): {STAGNANT_COOLDOWN - (now - last_stagnant_novelty):.0f}s remaining (avoiding oscillation)")
                elif novelty > CRISIS_NOVELTY_TARGET:
                    new_novelty = max(novelty - 0.03, CRISIS_NOVELTY_TARGET)
                    adjustments.append(f"EXTREME CRISIS CONSOLIDATION: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, bypass dead zone, consolidating)")
                    if not args.dry_run:
                        state["novelty_weight"] = new_novelty
                elif novelty < CRISIS_NOVELTY_TARGET:
                    new_novelty = min(novelty + 0.03, CRISIS_NOVELTY_TARGET)
                    adjustments.append(f"EXTREME CRISIS PUSH: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, bypass dead zone, pushing toward consolidation)")
                    if not args.dry_run:
                        state["novelty_weight"] = new_novelty
                else:
                    adjustments.append(f"Novelty at consolidation target: {novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target)")
            elif crisis_ratio >= 10:
                # DEEP CRISIS: narrow dead zone, consolidate harder toward target
                # STALEMATE BREAKER: If hidden bridges stable at 10x+ target for 3+
                # consecutive cycles with no change, narrow dead zones and force small
                # perturbations to break the local minimum.
                NOVELTY_CONSOLIDATE_LOW = 0.15  # Lowered to accommodate stagnant target (0.15)
                NOVELTY_CONSOLIDATE_HIGH = 0.30
                # STALEMATE BREAKER: enabled at deep crisis.
                # Disabling it creates dead zone equilibrium at stagnant targets.
                # At deep crisis with 222 hidden bridges, the system needs EXPLORATION
                # to surface hidden routes — consolidation concentrates traffic on known
                # routes and doesn't reduce hidden bridges.
                stalemate = False  # DISABLED at deep crisis - stalemate breaker pushes exploration when consolidation needed
                if False and len(hidden_history) >= 3:  # DISABLED - see auto-tune-stalemate-breaker-fix skill
                    h3 = hidden_history[-3:]
                    if max(h3) - min(h3) <= 5 and min(h3) >= HIDDEN_BRIDGE_TARGET * 10:
                        # Stable bridges at 10x+ target = consolidation failing, need perturbation
                        stalemate = True
                if stalemate:
                    STALEMATE_COOLDOWN = 360  # 6 minutes
                    last_stalemate_novelty = state.get("last_stalemate_time_novelty", 0)
                    if now - last_stalemate_novelty < STALEMATE_COOLDOWN:
                        adjustments.append(f"Stalemate breaker cooldown (novelty): {STALEMATE_COOLDOWN - (now - last_stalemate_novelty):.0f}s remaining (hidden={hidden} {crisis_ratio:.1f}x target, avoiding oscillation)")
                    else:
                        # System stuck — perturb OUTSIDE dead zone to break deadlock
                        # Push novelty LOWER (below dead zone) to reduce exploration noise
                        STALEMATE_NOVELTY_LOW = 0.15  # Below dead zone [0.20, 0.30]
                        STALEMATE_NOVELTY_HIGH = 0.35  # Above dead zone [0.20, 0.30]
                        if novelty > NOVELTY_CONSOLIDATE_HIGH:
                            # Already above dead zone — alternate direction
                            stalemate_count = len([h for h in hidden_history[-3:] if h == hidden_history[-1]])
                            if novelty < STALEMATE_NOVELTY_HIGH:
                                new_novelty = min(novelty + 0.03, STALEMATE_NOVELTY_HIGH)
                                adjustments.append(f"STALEMATE BREAKER: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target for 3+ cycles, pushing above dead zone)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            elif novelty > STALEMATE_NOVELTY_LOW:
                                # At ceiling — push DOWN on alternating cycles to break stalemate
                                new_novelty = max(novelty - 0.05, STALEMATE_NOVELTY_LOW)
                                adjustments.append(f"STALEMATE BREAKER: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, at ceiling — reversing toward floor)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            else:
                                adjustments.append(f"Novelty at stalemate ceiling: {novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, above dead zone)")
                        elif novelty < NOVELTY_CONSOLIDATE_LOW:
                            # Already below dead zone — alternate direction
                            stalemate_count = len([h for h in hidden_history[-3:] if h == hidden_history[-1]])
                            if novelty > STALEMATE_NOVELTY_LOW:
                                new_novelty = max(novelty - 0.03, STALEMATE_NOVELTY_LOW)
                                adjustments.append(f"STALEMATE BREAKER: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target for 3+ cycles, pushing below dead zone)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            elif novelty < STALEMATE_NOVELTY_HIGH:
                                # At floor — push UP on alternating cycles to break stalemate
                                new_novelty = min(novelty + 0.05, STALEMATE_NOVELTY_HIGH)
                                adjustments.append(f"STALEMATE BREAKER: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, at floor — reversing toward ceiling)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            else:
                                adjustments.append(f"Novelty at stalemate floor: {novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, below dead zone)")
                        else:
                            # Inside dead zone — alternate direction based on cycle count
                            stalemate_count = len([h for h in hidden_history[-3:] if h == hidden_history[-1]])
                            if stalemate_count % 2 == 0:
                                # Even cycle: push DOWN below dead zone
                                new_novelty = max(novelty - 0.05, STALEMATE_NOVELTY_LOW)
                                adjustments.append(f"STALEMATE BREAKER: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, pushing below dead zone)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            else:
                                # Odd cycle: push UP above dead zone
                                new_novelty = min(novelty + 0.05, STALEMATE_NOVELTY_HIGH)
                                adjustments.append(f"STALEMATE BREAKER: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, pushing above dead zone)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                        # Update last_stalemate_time_novelty after any stalemate adjustment
                        if not args.dry_run:
                            state["last_stalemate_time_novelty"] = now
                elif NOVELTY_CONSOLIDATE_LOW <= novelty <= NOVELTY_CONSOLIDATE_HIGH:
                    if any_stalemate_recently_perturbed(state, now):
                        adjustments.append(f"Novelty perturbation window: {novelty:.2f} (stalemate breaker recently fired, skipping consolidation)")
                    elif is_consolidation_stagnant(hidden_history, hidden):
                        # STAGNANT: hidden bridges flat at deep crisis for 3+ cycles.
                        # Tighten dead zone to force novelty toward lower consolidation target.
                        # Dead zone centered on target to prevent oscillation.
                        STAGNANT_NOVELTY_LOW = 0.13
                        STAGNANT_NOVELTY_HIGH = 0.17
                        STAGNANT_NOVELTY_TARGET = 0.15
                        if novelty > STAGNANT_NOVELTY_HIGH:
                            if r < 0 and novelty <= 0.25:
                                # DEADLOCK FIX: novelty in (0.17, 0.25] with r<0 — neither
                                # consolidation (too high) nor trap escape (not in dead zone)
                                # can act. Push UP slightly to restore routing.
                                new_novelty = min(novelty + 0.03, 0.25)
                                adjustments.append(f"STAGNANT TRAP ESCAPE: novelty {novelty:.2f} → {new_novelty:.2f} (flow r={r:.4f} < 0, no-man's-land escape — pushing toward exploration)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            else:
                                new_novelty = max(novelty - 0.03, STAGNANT_NOVELTY_TARGET)
                                adjustments.append(f"STAGNANT CONSOLIDATION: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, tightening dead zone)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                        elif novelty < STAGNANT_NOVELTY_LOW:
                            new_novelty = min(novelty + 0.03, STAGNANT_NOVELTY_LOW)
                            adjustments.append(f"STAGNANT CONSOLIDATION: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, tightening dead zone)")
                            if not args.dry_run:
                                state["novelty_weight"] = new_novelty
                        else:
                            # In dead zone but not at target — push toward target
                            if novelty > STAGNANT_NOVELTY_TARGET:
                                new_novelty = max(novelty - 0.02, STAGNANT_NOVELTY_TARGET)
                                adjustments.append(f"STAGNANT CONSOLIDATION: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, pushing toward target 0.15)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            elif novelty < STAGNANT_NOVELTY_TARGET:
                                new_novelty = min(novelty + 0.02, STAGNANT_NOVELTY_TARGET)
                                adjustments.append(f"STAGNANT CONSOLIDATION: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, pushing toward target 0.15)")
                                if not args.dry_run:
                                    state["novelty_weight"] = new_novelty
                            else:
                                # At stagnant target but flow is dead — consolidation trap
                                # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                                escape_threshold_deep_novelty = -0.05 if crisis_ratio >= 5 else 0
                                if r < escape_threshold_deep_novelty:
                                    new_novelty = min(novelty + 0.05, 0.25)
                                    adjustments.append(f"DEEP CRISIS CONSOLIDATION TRAP ESCAPE: novelty {novelty:.2f} → {new_novelty:.2f} (flow r={r:.4f} < 0, hidden={hidden} flat at {crisis_ratio:.1f}x target, restoring routing)")
                                    if not args.dry_run:
                                        state["novelty_weight"] = new_novelty
                                elif r < 0.005:
                                    new_novelty = min(novelty + 0.03, 0.22)
                                    adjustments.append(f"DEEP CRISIS MIN-ROUTING: novelty {novelty:.2f} → {new_novelty:.2f} (flow r={r:.4f} < 0.005, hidden={hidden} flat at {crisis_ratio:.1f}x target, restoring minimal routing)")
                                    if not args.dry_run:
                                        state["novelty_weight"] = new_novelty
                                else:
                                    adjustments.append(f"Novelty settled (stagnant): {novelty:.2f} (at target 0.15, hidden={hidden} flat at {crisis_ratio:.1f}x target)")
                    else:
                        adjustments.append(f"Novelty settled (deep crisis): {novelty:.2f} (dead zone [{NOVELTY_CONSOLIDATE_LOW}, {NOVELTY_CONSOLIDATE_HIGH}], hidden={hidden} {crisis_ratio:.1f}x target)")
                elif novelty > NOVELTY_CONSOLIDATE_HIGH:
                    # Above consolidation zone — push DOWN
                    if any_stalemate_recently_perturbed(state, now):
                        adjustments.append(f"Novelty perturbation window: {novelty:.2f} (stalemate breaker recently fired, skipping consolidation)")
                    else:
                        new_novelty = max(novelty - 0.03, NOVELTY_CONSOLIDATE_HIGH)
                        adjustments.append(f"DEEP CRISIS CONSOLIDATION: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, consolidating)")
                        if not args.dry_run:
                            state["novelty_weight"] = new_novelty
                else:
                    # Below consolidation zone — push UP
                    if any_stalemate_recently_perturbed(state, now):
                        adjustments.append(f"Novelty perturbation window: {novelty:.2f} (stalemate breaker recently fired, skipping consolidation)")
                    else:
                        new_novelty = min(novelty + 0.03, NOVELTY_CONSOLIDATE_LOW)
                        adjustments.append(f"DEEP CRISIS PUSH: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, pushing toward consolidation)")
                        if not args.dry_run:
                            state["novelty_weight"] = new_novelty
            else:
                # NORMAL CRISIS: tight dead zone centered on consolidation target.
                # Narrowed from [0.15, 0.35] — old range included 0.20, freezing novelty
                # while hidden bridges sat at 7.8x target. Dead zone now [0.27, 0.33]
                # (±0.03 around target 0.30) so params outside get pushed toward target.
                NOVELTY_CRISIS_SETTLED_LOW = 0.27
                NOVELTY_CRISIS_SETTLED_HIGH = 0.33

                # STAGNANT DETECTION for 5-10x crisis band.
                # When hidden bridges flat for 5+ cycles at crisis level,
                # force movement — current parameters aren't working.
                normal_crisis_stagnant = False
                if len(hidden_history) >= 5:
                    recent5 = hidden_history[-5:]
                    h_range = max(recent5) - min(recent5)
                    h_min = min(recent5)
                    h_pct_range = h_range / h_min if h_min > 0 else 999
                    if h_pct_range < 0.03 and h_min >= HIDDEN_BRIDGE_TARGET * 5:
                        normal_crisis_stagnant = True

                if normal_crisis_stagnant:
                    # Hidden bridges flat at crisis level — force novelty toward target
                    # regardless of dead zone. Current parameters aren't reducing bridges.
                    STAGNANT_NOVELTY_TARGET = CRISIS_NOVELTY_TARGET  # 0.20
                    if abs(novelty - STAGNANT_NOVELTY_TARGET) < 0.03:
                        # Already near target — DEADLOCK BREAKER check
                        # If hidden bridges flat for 5+ cycles and novelty at target,
                        # we need to force injection or trust to move instead.
                        deadlock_count = state.get('normal_crisis_deadlock_count', 0)
                        state['normal_crisis_deadlock_count'] = deadlock_count + 1
                        
                        if deadlock_count >= 7:
                            # DEEP DEADLOCK ESCAPE: all params at extremes for 7+ cycles,
                            # hidden bridges flat, flow r declining.
                            # First check if pushing toward breakthrough values would be a no-op
                            # (params already at targets). If so, try STRATEGY REVERSAL instead.
                            target_inject = min(current_inject + 3, CRISIS_INJECTION_TARGET)
                            target_trust = max(trust - 0.05, CRISIS_TRUST_TARGET)
                            target_novelty = CRISIS_NOVELTY_TARGET
                            is_noop = (target_inject == current_inject and
                                       target_trust == trust and
                                       target_novelty == novelty)
                            if is_noop:
                                # STRATEGY REVERSAL: current config (high injection, low trust)
                                # has failed for 7+ cycles. Try the opposite approach:
                                # - Reduce injection (15→8): less flooding, more targeted routing
                                # - Raise trust (0.05→0.15): concentrate on proven-good paths
                                # - Increase novelty (0.20→0.25): moderate exploration
                                new_inject = max(current_inject - 7, 8)
                                new_trust = min(trust + 0.10, 0.15)
                                new_novelty = min(novelty + 0.05, 0.25)
                                adjustments.append(f"DEEP DEADLOCK ESCAPE (STRATEGY REVERSAL): novelty {novelty:.2f}→{new_novelty:.2f}, injection {current_inject}→{new_inject}, trust {trust:.2f}→{new_trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target for {deadlock_count+1} cycles, current config FAILED — reversing to concentrate traffic)")
                            else:
                                # Push toward breakthrough values
                                new_inject = target_inject
                                new_trust = target_trust
                                new_novelty = target_novelty
                                adjustments.append(f"DEEP DEADLOCK ESCAPE: novelty {novelty:.2f}→{new_novelty:.2f}, injection {current_inject}→{new_inject}, trust {trust:.2f}→{new_trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target for {deadlock_count+1} cycles, pushing toward breakthrough values)")
                            if not args.dry_run:
                                state["novelty_weight"] = new_novelty
                                state["injection_rate"] = new_inject
                                adjust_injection_rate(new_inject)
                                state["trust_weight"] = new_trust
                                # LOCK: Prevent injection/trust sections from overriding for 10 minutes
                                state["deep_deadlock_escape_until"] = now + DEEP_DEADLOCK_COOLDOWN
                            state['normal_crisis_deadlock_count'] = 0
                        elif deadlock_count >= 2:
                            # DEADLOCK BREAKER: 3+ consecutive holds at crisis level
                            # Fix 12: Don't consolidate injection/trust here — the injection
                            # and trust sections have their own DEADLOCK EXHAUSTED OVERRIDE
                            # that pushes injection toward ceiling and trust toward floor.
                            # Consolidating here would UNDO their push (oscillation).
                            # Just hold and let injection/trust sections handle movement.
                            adjustments.append(f"NORMAL CRISIS DEADLOCK BREAKER: novelty {novelty:.2f} HOLD (hidden={hidden} flat {crisis_ratio:.1f}x target for {deadlock_count+1} cycles, delegating injection/trust to exhausted overrides)")
                        else:
                            adjustments.append(f"NORMAL CRISIS STAGNANT HOLD: novelty {novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, at target, deadlock_count={deadlock_count+1})")
                    elif novelty < STAGNANT_NOVELTY_TARGET:
                        new_novelty = min(novelty + 0.05, STAGNANT_NOVELTY_TARGET)
                        adjustments.append(f"NORMAL CRISIS STAGNANT PUSH: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, forcing toward target)")
                        if not args.dry_run:
                            state["novelty_weight"] = new_novelty
                        state['normal_crisis_deadlock_count'] = 0
                    else:
                        # DEEP DEADLOCK ESCAPE LOCK: when DEEP DEADLOCK ESCAPE just fired,
                        # respect its novelty value for the cooldown period.
                        deep_escape_until = state.get('deep_deadlock_escape_until', 0)
                        deep_escape_active = now < deep_escape_until
                        if deep_escape_active:
                            remaining = (deep_escape_until - now) / 60
                            adjustments.append(f"NOVELTY LOCK (deep deadlock escape): novelty {novelty:.2f} (locked for {remaining:.1f}min, respecting strategy reversal)")
                        else:
                            new_novelty = max(novelty - 0.05, STAGNANT_NOVELTY_TARGET)
                            adjustments.append(f"NORMAL CRISIS STAGNANT CONSOLIDATE: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, forcing toward target)")
                            if not args.dry_run:
                                state["novelty_weight"] = new_novelty
                            state['normal_crisis_deadlock_count'] = 0
                elif NOVELTY_CRISIS_SETTLED_LOW <= novelty <= NOVELTY_CRISIS_SETTLED_HIGH:
                    adjustments.append(f"Novelty settled: {novelty:.2f} (crisis dead zone [{NOVELTY_CRISIS_SETTLED_LOW}, {NOVELTY_CRISIS_SETTLED_HIGH}], hidden={hidden} {crisis_ratio:.1f}x target)")
                elif novelty >= NOVELTY_AT_EXTREME:
                    # Parameters at extreme — consolidate to break saturation
                    if novelty > CRISIS_NOVELTY_TARGET:
                        new_novelty = max(novelty - 0.03, CRISIS_NOVELTY_TARGET)
                        adjustments.append(f"CRISIS CONSOLIDATION: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} crisis, params at extreme — consolidation)")
                        if not args.dry_run:
                            state["novelty_weight"] = new_novelty
                elif novelty < NOVELTY_CRISIS_SETTLED_LOW:
                    # Below dead zone — push toward consolidation target (not exploration)
                    new_novelty = min(novelty + 0.03, CRISIS_NOVELTY_TARGET)
                    adjustments.append(f"CRISIS NOVELTY PUSH: novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, pushing toward consolidation target {CRISIS_NOVELTY_TARGET})")
                    if not args.dry_run:
                        state["novelty_weight"] = new_novelty
                else:
                    # Parameters NOT at extreme — push exploration even in stable crisis
                    CRISIS_EXPLORE_CAP = 0.45
                    if novelty < CRISIS_EXPLORE_CAP:
                        new_novelty = min(novelty + 0.03, CRISIS_EXPLORE_CAP)
                        adjustments.append(f"CRISIS EXPLORATION PUSH (stable): novelty {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} crisis, params mid-range — need exploration)")
                        if not args.dry_run:
                            state["novelty_weight"] = new_novelty
                    else:
                        adjustments.append(f"Novelty at crisis explore cap: {novelty:.2f} (hidden={hidden} crisis, params at cap)")
        elif hidden_growing:
            # Hidden bridges GROWING despite extreme params → push exploration harder
            if state["novelty_weight"] < 0.45:
                new_novelty = min(state["novelty_weight"] + 0.03, 0.45)
                adjustments.append(f"EXPLORATION PUSH (growing): novelty {state['novelty_weight']:.2f} → {new_novelty:.2f} (hidden={hidden} growing, need more exploration)")
                if not args.dry_run:
                    state["novelty_weight"] = new_novelty
            else:
                adjustments.append(f"Novelty at exploration cap: {state['novelty_weight']:.2f} (hidden={hidden} growing)")
        elif state["novelty_weight"] > NOVELTY_EXTREME_HIGH:
            # High novelty with extreme hidden bridges AND stable — consolidate
            new_novelty = max(state["novelty_weight"] - 0.05, NOVELTY_WEIGHT_MIN + 0.05)
            if new_novelty != state["novelty_weight"]:
                adjustments.append(f"CONSOLIDATION (extreme): novelty {state['novelty_weight']:.2f} → {new_novelty:.2f} (hidden={hidden} stable — above dead zone)")
                if not args.dry_run:
                    state["novelty_weight"] = new_novelty
        elif state["novelty_weight"] < NOVELTY_EXTREME_LOW:
            # Below dead zone — increase toward it
            new_novelty = min(state["novelty_weight"] + 0.03, NOVELTY_EXTREME_LOW)
            adjustments.append(f"CONSOLIDATION (extreme): novelty {state['novelty_weight']:.2f} → {new_novelty:.2f} (hidden={hidden} stable — below dead zone, nudging up)")
            if not args.dry_run:
                state["novelty_weight"] = new_novelty
        else:
            # In dead zone AND stable — no change
            adjustments.append(f"Novelty settled: {state['novelty_weight']:.2f} (extreme_high dead zone, hidden={hidden} stable)")
    elif hidden > HIDDEN_BRIDGE_TARGET:
        novelty = state["novelty_weight"]
        if NOVELTY_SETTLED_LOW <= novelty <= NOVELTY_SETTLED_HIGH:
            # Above target but within settled range — push to upper bound to increase exploration
            # FIX (2026-06-20): Previous code just `pass`ed here, creating a dead zone
            # where hidden bridges 15-45 received zero parameter response.
            if novelty < NOVELTY_SETTLED_HIGH:
                new_novelty = min(novelty + 0.03, NOVELTY_SETTLED_HIGH)
                adjustments.append(f"Increase novelty (above-target settled): {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target, pushing within settled range)")
                if not args.dry_run:
                    state["novelty_weight"] = new_novelty
            else:
                adjustments.append(f"Novelty at settled high: {novelty:.2f} (hidden={hidden} {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target, at upper settled bound)")
        elif novelty > NOVELTY_SETTLED_HIGH:
            # Above settled range — decrease toward it
            new_novelty = max(novelty - 0.02, NOVELTY_SETTLED_LOW)
            adjustments.append(f"Decrease novelty: {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} > target)")
            if not args.dry_run:
                state["novelty_weight"] = new_novelty
        elif novelty < NOVELTY_SETTLED_LOW:
            # Below settled range — increase toward it
            new_novelty = min(novelty + 0.05, NOVELTY_SETTLED_HIGH)
            adjustments.append(f"Increase novelty: {novelty:.2f} → {new_novelty:.2f} (hidden={hidden} > target)")
            if not args.dry_run:
                state["novelty_weight"] = new_novelty
    else:
        # HIDDEN_BRIDGE_MIN <= hidden <= HIDDEN_BRIDGE_TARGET: healthy range
        # Hidden bridges between min (5) and target (15) — system is in good shape.
        # Hold novelty at current value; no adjustment needed.
        novelty = state["novelty_weight"]
        adjustments.append(f"Novelty hold: {novelty:.2f} (hidden={hidden} in healthy range [{HIDDEN_BRIDGE_MIN}-{HIDDEN_BRIDGE_TARGET}])")

    ctx.novelty = novelty
    ctx.trust = trust
    ctx.current_inject = current_inject
    ctx.extreme_high = extreme_high
    ctx.hidden_growing = hidden_growing
    ctx.hidden_history = hidden_history
    ctx.normal_crisis_stagnant = normal_crisis_stagnant


def _tune_injection_rate(ctx, args):
    """Phase 3 — injection rate. Publishes to ctx: current_inject."""
    state = ctx.state
    adjustments = ctx.adjustments
    r = ctx.r
    outcome_r = ctx.outcome_r
    hidden = ctx.hidden
    routing_collapsed = ctx.routing_collapsed
    total_edges = ctx.total_edges
    zero_flow_edges = ctx.zero_flow_edges
    now = ctx.now
    extreme_high = ctx.extreme_high
    hidden_growing = ctx.hidden_growing
    hidden_history = ctx.hidden_history
    normal_crisis_stagnant = ctx.normal_crisis_stagnant

    # 3. Adjust injection rate based on queue growth
    # Read from state file (authoritative source) instead of file content regex
    # The file has a default fallback of 5, but the actual rate comes from state
    current_inject = state.get("injection_rate", 5)
    inject_path = HERMES / "scripts" / "inject_opportunities.py"
    if inject_path.exists():

        if hidden < HIDDEN_BRIDGE_MIN and current_inject < INJECTION_RATE_MAX:
            # Zero hidden bridges: increase injection to discover new connections
            # When flow r < 0, injection is especially needed (under-exploration)
            if hidden == 0:
                if r < 0:
                    # Flow negative + hidden=0 = under-exploration. Increase injection.
                    new_inject = min(current_inject + 1, INJECTION_RATE_MAX)
                    adjustments.append(f"Increase injection: {current_inject} → {new_inject} (hidden=0, flow r={r:.4f} < 0, under-exploration)")
                    if not args.dry_run:
                        state["injection_rate"] = new_inject
                        adjust_injection_rate(new_inject)
                else:
                    # Flow non-negative + hidden=0 = discovery stalled but not urgent.
                    adjustments.append(f"Injection HOLD (hidden=0, flow r={r:.4f} >= 0): keeping at {current_inject}")
            elif outcome_r >= 0.80:
                # Low but non-zero hidden bridges with healthy outcome_r — system is finding some connections
                adjustments.append(f"Hidden bridges low ({hidden}) but outcome_r={outcome_r:.3f} — monitoring, no injection increase")
            else:
                new_inject = min(current_inject + 1, INJECTION_RATE_MAX)
                adjustments.append(f"Increase injection: {current_inject} → {new_inject}")
                if not args.dry_run:
                    state["injection_rate"] = new_inject
                    adjust_injection_rate(new_inject)
        elif extreme_high:
            # Extreme high hidden bridges — adjust injection based on trend.
            # Hidden bridges are high-success low-traffic edges. More injection
            # = more experiments = more chances to route traffic to these edges.
            crisis = hidden >= HIDDEN_BRIDGE_CRISIS
            if crisis and hidden_growing:
                # CRISIS + GROWING: adjust injection based on crisis severity.
                # At 10x+ target (deep crisis), all params at extremes = saturation loop.
                # Consolidate injection toward target to break the feedback.
                crisis_ratio = hidden / HIDDEN_BRIDGE_TARGET
                if crisis_ratio >= 10:
                    # DEEP CRISIS + GROWING: consolidate injection toward target
                    # FIX: High injection is often the CAUSE of negative flow r
                    # (injecting many failing edges drags flow r down). Always
                    # consolidate injection when above target, even if flow r < 0.
                    if current_inject > CRISIS_INJECTION_TARGET:
                        new_inject = max(current_inject - 1, CRISIS_INJECTION_TARGET)
                        adjustments.append(f"DEEP CRISIS CONSOLIDATION (growing): injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, breaking saturation)")
                        if not args.dry_run:
                            state["injection_rate"] = new_inject
                            adjust_injection_rate(new_inject)
                    elif current_inject < CRISIS_INJECTION_TARGET:
                        new_inject = min(current_inject + 1, CRISIS_INJECTION_TARGET)
                        adjustments.append(f"DEEP CRISIS PUSH (growing): injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, pushing toward consolidation)")
                        if not args.dry_run:
                            state["injection_rate"] = new_inject
                            adjust_injection_rate(new_inject)
                    else:
                        adjustments.append(f"Injection at consolidation target: {current_inject} (hidden={hidden} {crisis_ratio:.1f}x target, deep crisis)")
                else:
                    # NORMAL CRISIS + GROWING: push injection higher to surface bridges
                    # FIX: Ceiling was 10, below CRISIS_INJECTION_TARGET (15). At 8x target,
                    # injection=12 was reported as "at ceiling" when it should be pushed to 15.
                    CRISIS_INJECT_CEILING = INJECTION_RATE_MAX  # Match crisis target
                    if current_inject < CRISIS_INJECT_CEILING:
                        new_inject = min(current_inject + 1, CRISIS_INJECT_CEILING)
                        adjustments.append(f"CRISIS INJECTION PUSH (growing): injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, faster bridge surfacing)")
                        if not args.dry_run:
                            state["injection_rate"] = new_inject
                            adjust_injection_rate(new_inject)
                    else:
                        adjustments.append(f"Injection at crisis explore ceiling: {current_inject} (hidden={hidden} {crisis_ratio:.1f}x target, growing)")
            elif crisis:
                # CRISIS + STABLE: Only consolidate if injection is at exploration extreme.
                # If injection is at mid-range, push higher to surface bridges faster.
                INJECTION_AT_EXTREME = 12  # Raised from 8 — at 7.8x crisis, injection=10 is NOT extreme
                # Dead zone width scales with crisis severity:
                # - 5x target (75): dead zone within 1 of target (prevent oscillation)
                # - 10x target (150): dead zone within 0 of target (push exploration)
                # - 15x+ target (225+): bypass dead zone entirely (extreme crisis)
                crisis_ratio = hidden / HIDDEN_BRIDGE_TARGET
                if crisis_ratio >= 15:
                    # EXTREME CRISIS: bypass dead zone, consolidate toward target
                    # STALEMATE BREAKER: DISABLED at extreme crisis (>=15x target)
                    # System needs hard consolidation, not exploration perturbation.
                    # STAGNANT DETECTION: If hidden bridges flat for 5+ cycles at target,
                    # break dead zone with small perturbation.
                    extreme_stagnant = False
                    if len(hidden_history) >= 5:
                        recent5 = hidden_history[-5:]
                        h_range = max(recent5) - min(recent5)
                        h_min = min(recent5)
                        h_pct_range = h_range / h_min if h_min > 0 else 999
                        if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15:
                            extreme_stagnant = True

                    STAGNANT_COOLDOWN_INJECT = 120  # 2 minutes (reduced for crisis responsiveness)
                    last_stagnant_injection = state.get("last_stagnant_time_injection", 0)
                    stagnant_injection_cooldown = now - last_stagnant_injection < STAGNANT_COOLDOWN_INJECT

                    # Stagnant injection target aligned with crisis target
                    STAGNANT_EXTREME_INJECT = 3  # Aligned with CRISIS_INJECTION_TARGET
                    if extreme_stagnant and not stagnant_injection_cooldown:
                        if current_inject < STAGNANT_EXTREME_INJECT:
                            new_inject = min(current_inject + 1, STAGNANT_EXTREME_INJECT)
                            adjustments.append(f"EXTREME STAGNANT PERTURB: injection {current_inject} \u2192 {new_inject} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, breaking dead zone)")
                            if not args.dry_run:
                                state["injection_rate"] = new_inject
                                adjust_injection_rate(new_inject)
                                state["last_stagnant_time_injection"] = now
                        elif current_inject > STAGNANT_EXTREME_INJECT:
                            # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                            escape_threshold_inject = -0.05 if crisis_ratio >= 5 else 0
                            if r < escape_threshold_inject:
                                # Flow deeply negative — consolidation has killed routing.
                                # Push injection UP to escape consolidation trap.
                                # Cap at 10 (max) not 6 — at 6 routing is still degraded.
                                new_inject = min(current_inject + 2, INJECTION_RATE_MAX)
                                adjustments.append(f"CONSOLIDATION TRAP ESCAPE: injection {current_inject} → {new_inject} (flow r={r:.4f} < 0, pushing toward exploration to restore routing)")
                                if not args.dry_run:
                                    state["injection_rate"] = new_inject
                                    adjust_injection_rate(new_inject)
                                    state["last_stagnant_time_injection"] = now
                            elif r < 0.01:
                                # Flow critically low but not negative.
                                # CRISIS OVERRIDE: When hidden bridges flat at 18x+ target,
                                # consolidation is MORE important than routing capacity.
                                # STAGNANT CRISIS ESCAPE: If hidden bridges flat for 5+ cycles,
                                # consolidation has FAILED. Push UP to try exploration.
                                if crisis_ratio >= 18 and current_inject > STAGNANT_EXTREME_INJECT + 1:
                                    consolidation_stuck = False
                                    if len(hidden_history) >= 5:
                                        recent5 = hidden_history[-5:]
                                        h_range = max(recent5) - min(recent5)
                                        h_min = min(recent5)
                                        h_pct_range = h_range / h_min if h_min > 0 else 999
                                        if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15:
                                            consolidation_stuck = True
                                    if consolidation_stuck:
                                        # FIX 2026-06-15: Cap was 7 → no-op. Now at INJECTION_RATE_MAX.
                                        # Injection is genuinely maxed — cannot increase further.
                                        if current_inject < INJECTION_RATE_MAX:
                                            new_inject = min(current_inject + 2, INJECTION_RATE_MAX)
                                            adjustments.append(f"STAGNANT CRISIS ESCAPE (consolidation failed): injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target flat for 5+ cycles, consolidation not working, trying exploration)")
                                            if not args.dry_run:
                                                state["injection_rate"] = new_inject
                                                adjust_injection_rate(new_inject)
                                                state["last_stagnant_time_injection"] = now
                                        else:
                                            # DEADLOCK BREAKER: Injection at ceiling, can't increase.
                                            # Flip to hard consolidation — force injection DOWN toward crisis target.
                                            new_inject = max(current_inject - 3, CRISIS_INJECTION_TARGET)
                                            adjustments.append(f"DEADLOCK BREAKER: injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, injection maxed & consolidation failed, forcing hard consolidation)")
                                            if not args.dry_run:
                                                state["injection_rate"] = new_inject
                                                adjust_injection_rate(new_inject)
                                                state["last_stagnant_time_injection"] = now
                                    else:
                                        new_inject = max(current_inject - 1, STAGNANT_EXTREME_INJECT)
                                        adjustments.append(f"EXTREME CRISIS CONSOLIDATION (flow override): injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target flat, consolidating despite low flow)")
                                        if not args.dry_run:
                                            state["injection_rate"] = new_inject
                                            adjust_injection_rate(new_inject)
                                            state["last_stagnant_time_injection"] = now
                                elif current_inject <= STAGNANT_EXTREME_INJECT + 4:
                                    # CRISIS GUARD: At 18x+ target, consolidation overrides routing
                                    if crisis_ratio >= 18:
                                        # ROUTING COLLAPSE ESCAPE: When ALL flow is zero, consolidation
                                        # has killed routing entirely. Push params UP to restore routing.
                                        if routing_collapsed:
                                            new_inject = min(current_inject + 1, 6)
                                            adjustments.append(f"ROUTING COLLAPSE ESCAPE: injection {current_inject} → {new_inject} (routing dead — {zero_flow_edges}/{total_edges} edges zero flow, restoring routing to break deadlock)")
                                            if not args.dry_run:
                                                state["injection_rate"] = new_inject
                                                adjust_injection_rate(new_inject)
                                                state["last_stagnant_time_injection"] = now
                                        else:
                                            adjustments.append(f"EXTREME CRISIS HOLD (consolidation priority): injection {current_inject} (hidden={hidden} {crisis_ratio:.1f}x target, consolidation overrides routing)")
                                    else:
                                        new_inject = min(current_inject + 1, 7)
                                        adjustments.append(f"EXTREME STAGNANT MIN-ROUTING (near target): injection {current_inject} → {new_inject} (flow r={r:.4f} < 0.01, restoring minimal routing)")
                                        if not args.dry_run:
                                            state["injection_rate"] = new_inject
                                            adjust_injection_rate(new_inject)
                                            state["last_stagnant_time_injection"] = now
                                else:
                                    adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): injection {current_inject} (flow r={r:.4f} < 0.005, holding above target to preserve routing)")
                            else:
                                # Only PERTURB DOWN when flow r is healthy enough.
                                # At r < 0.01, system still needs more routing capacity.
                                if r >= 0.01:
                                    new_inject = max(current_inject - 1, STAGNANT_EXTREME_INJECT)
                                    adjustments.append(f"EXTREME STAGNANT PERTURB: injection {current_inject} \u2192 {new_inject} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, breaking dead zone)")
                                    if not args.dry_run:
                                        state["injection_rate"] = new_inject
                                        adjust_injection_rate(new_inject)
                                        state["last_stagnant_time_injection"] = now
                                else:
                                    adjustments.append(f"EXTREME STAGNANT HOLD (flow low): injection {current_inject} (flow r={r:.4f} < 0.01, preserving routing capacity)")
                        else:
                            # At stagnant target — check for minimum viable routing
                            # When flow r is critically low, system is paralyzed. Push injection
                            # slightly above stagnant target to restore minimal cross-domain activity.
                            if r < 0.01:
                                # CRISIS GUARD: At 18x+ target, consolidation overrides routing
                                if crisis_ratio >= 18:
                                    # ROUTING COLLAPSE ESCAPE: When ALL flow is zero, consolidation
                                    # has killed routing entirely. Push params UP to restore routing.
                                    if routing_collapsed:
                                        new_inject = min(current_inject + 1, 6)
                                        adjustments.append(f"ROUTING COLLAPSE ESCAPE: injection {current_inject} → {new_inject} (routing dead — {zero_flow_edges}/{total_edges} edges zero flow, restoring routing to break deadlock)")
                                        if not args.dry_run:
                                            state["injection_rate"] = new_inject
                                            adjust_injection_rate(new_inject)
                                            state["last_stagnant_time_injection"] = now
                                    else:
                                        # DEADLOCK FIX: At stagnant target with flat hidden bridges,
                                        # consolidation has FAILED. Force exploration to break local minimum.
                                        if extreme_stagnant:
                                            new_inject = min(current_inject + 2, INJECTION_RATE_MAX)
                                            if new_inject <= current_inject:
                                                adjustments.append(f"INJECTION DEADLOCK: injection {current_inject} (at stagnant target, exploration maxed, holding)")
                                            else:
                                                adjustments.append(f"STAGNANT TARGET ESCAPE (consolidation failed): injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target flat for 5+ cycles, at floor — forcing exploration)")
                                                if not args.dry_run:
                                                    state["injection_rate"] = new_inject
                                                    adjust_injection_rate(new_inject)
                                                    state["last_stagnant_time_injection"] = now
                                        else:
                                            adjustments.append(f"EXTREME CRISIS HOLD (consolidation priority): injection {current_inject} (at stagnant target, consolidation overrides routing)")
                                else:
                                    new_inject = min(current_inject + 1, 6)
                                    adjustments.append(f"EXTREME STAGNANT MIN-ROUTING: injection {current_inject} → {new_inject} (flow r={r:.4f} < 0.01, restoring minimal routing)")
                                    if not args.dry_run:
                                        state["injection_rate"] = new_inject
                                        adjust_injection_rate(new_inject)
                                        state["last_stagnant_time_injection"] = now
                            else:
                                # HOLD (don't push further, prevents bounce-back oscillation)
                                adjustments.append(f"EXTREME STAGNANT HOLD: injection {current_inject} (at stagnant target, holding)")
                    elif extreme_stagnant and stagnant_injection_cooldown:
                        # Crisis bypass: when flow r critically low, bypass stagnant cooldown
                        if r < 0.01:
                            adjustments.append(f"CRISIS BYPASS: injection stagnant cooldown bypassed (flow r={r:.4f} < 0.01, extreme stagnant)")
                            # FIX: Flow r < 0 means consolidation has KILLED routing.
                            # Pushing DOWN further makes it worse. ALWAYS escape first.
                            if current_inject > STAGNANT_EXTREME_INJECT:
                                # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                                escape_threshold_inject_bypass = -0.05 if crisis_ratio >= 5 else 0
                                if r < escape_threshold_inject_bypass:
                                    # Flow deeply negative — consolidation trap. Push UP.
                                    new_inject = min(current_inject + 1, 6)
                                    adjustments.append(f"CONSOLIDATION TRAP ESCAPE (bypass): injection {current_inject} → {new_inject} (flow r={r:.4f} < 0, pushing toward exploration)")
                                    if not args.dry_run:
                                        state["injection_rate"] = new_inject
                                        adjust_injection_rate(new_inject)
                                        state["last_stagnant_time_injection"] = now
                                elif routing_collapsed:
                                    # ROUTING COLLAPSE: all flow zero — push UP to restore routing
                                    new_inject = min(current_inject + 1, 6)
                                    adjustments.append(f"ROUTING COLLAPSE ESCAPE (bypass): injection {current_inject} → {new_inject} (routing dead, restoring routing to break deadlock)")
                                    if not args.dry_run:
                                        state["injection_rate"] = new_inject
                                        adjust_injection_rate(new_inject)
                                        state["last_stagnant_time_injection"] = now
                                else:
                                    # DEADLOCK BREAKER (bypass): params far above consolidation targets
                                    if hidden_history and len(hidden_history) >= 5:
                                        recent5 = hidden_history[-5:]
                                        h_range = max(recent5) - min(recent5)
                                        h_min = min(recent5)
                                        h_pct_range = h_range / h_min if h_min > 0 else 999
                                        if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15 and current_inject >= STAGNANT_EXTREME_INJECT + 5:
                                            new_inject = max(current_inject - 3, STAGNANT_EXTREME_INJECT)
                                            adjustments.append(f"DEADLOCK BREAKER (bypass): injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target flat 5+ cycles, params far above target, forcing consolidation)")
                                            if not args.dry_run:
                                                state["injection_rate"] = new_inject
                                                adjust_injection_rate(new_inject)
                                                state["last_stagnant_time_injection"] = now
                                        else:
                                            adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): injection {current_inject} (flow r critically low, holding above target to preserve routing)")
                                    else:
                                        adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): injection {current_inject} (flow r critically low, holding above target to preserve routing)")
                            elif current_inject < STAGNANT_EXTREME_INJECT:
                                new_inject = min(current_inject + 1, STAGNANT_EXTREME_INJECT)
                                adjustments.append(f"EXTREME STAGNANT PERTURB (crisis): injection {current_inject} → {new_inject} (flow r critically low, bypassing cooldown)")
                                if not args.dry_run:
                                    state["injection_rate"] = new_inject
                                    adjust_injection_rate(new_inject)
                                    state["last_stagnant_time_injection"] = now
                            else:
                                adjustments.append(f"EXTREME STAGNANT HOLD: injection {current_inject} (at stagnant target, holding)")
                        else:
                            adjustments.append(f"Stagnant perturbation cooldown (injection): {STAGNANT_COOLDOWN_INJECT - (now - last_stagnant_injection):.0f}s remaining (avoiding oscillation)")
                    elif current_inject > CRISIS_INJECTION_TARGET:
                        new_inject = max(current_inject - 1, CRISIS_INJECTION_TARGET)
                        adjustments.append(f"EXTREME CRISIS CONSOLIDATION: injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, bypass dead zone, consolidating)")
                        if not args.dry_run:
                            state["injection_rate"] = new_inject
                            adjust_injection_rate(new_inject)
                    else:
                        adjustments.append(f"Injection at consolidation target: {current_inject} (hidden={hidden} {crisis_ratio:.1f}x target)")
                elif crisis_ratio >= 10:
                    # DEEP CRISIS: narrow dead zone (exactly at target)
                    # STALEMATE BREAKER: enabled at deep crisis (prevents dead zone equilibrium)
                    stalemate = False  # DISABLED at deep crisis - stalemate breaker pushes exploration when consolidation needed
                    if False and len(hidden_history) >= 3:  # DISABLED - see auto-tune-stalemate-breaker-fix skill
                        h3 = hidden_history[-3:]
                        if max(h3) - min(h3) <= 5 and min(h3) >= HIDDEN_BRIDGE_TARGET * 10:
                            # Stable bridges at 10x+ target = consolidation failing, need perturbation
                            stalemate = True
                    if stalemate:
                        STALEMATE_COOLDOWN = 360  # 6 minutes
                        last_stalemate_injection = state.get("last_stalemate_time_injection", 0)
                        if now - last_stalemate_injection < STALEMATE_COOLDOWN:
                            adjustments.append(f"Stalemate breaker cooldown (injection): {STALEMATE_COOLDOWN - (now - last_stalemate_injection):.0f}s remaining")
                        else:
                            # System stuck — alternate injection direction to break deadlock
                            stalemate_count = len([h for h in hidden_history[-3:] if h == hidden_history[-1]])
                            if stalemate_count % 2 == 0:
                                # Even cycle: push injection DOWN (below target)
                                new_inject = max(current_inject - 2, INJECTION_RATE_MIN)
                                adjustments.append(f"STALEMATE BREAKER: injection {current_inject} → {new_inject} (hidden={hidden} stable at {crisis_ratio:.1f}x target, reducing injection)")
                                if not args.dry_run:
                                    state["injection_rate"] = new_inject
                                    adjust_injection_rate(new_inject)
                            else:
                                # Odd cycle: push injection UP (above target)
                                new_inject = min(current_inject + 2, INJECTION_RATE_MAX)
                                if new_inject == current_inject:
                                    # Already at max — reverse direction instead of no-op
                                    new_inject = max(current_inject - 2, INJECTION_RATE_MIN)
                                    adjustments.append(f"STALEMATE BREAKER: injection {current_inject} → {new_inject} (hidden={hidden} stable at {crisis_ratio:.1f}x target, at max — reversing direction)")
                                else:
                                    adjustments.append(f"STALEMATE BREAKER: injection {current_inject} → {new_inject} (hidden={hidden} stable at {crisis_ratio:.1f}x target, boosting injection)")
                                if not args.dry_run:
                                    state["injection_rate"] = new_inject
                                    adjust_injection_rate(new_inject)
                            # Update last_stalemate_time_injection after any stalemate adjustment
                            if not args.dry_run:
                                state["last_stalemate_time_injection"] = now
                    elif current_inject == CRISIS_INJECTION_TARGET:
                        if any_stalemate_recently_perturbed(state, now):
                            adjustments.append(f"Injection perturbation window: {current_inject} (stalemate breaker recently fired, skipping consolidation)")
                        elif is_consolidation_stagnant(hidden_history, hidden):
                            # STAGNANT: hidden bridges flat at deep crisis. Injection at target is fine,
                            # but note the stagnation for monitoring purposes.
                            # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                            escape_threshold_deep_inject = -0.05 if crisis_ratio >= 5 else 0
                            if r < escape_threshold_deep_inject:
                                new_inject = min(current_inject + 2, 7)
                                adjustments.append(f"DEEP CRISIS CONSOLIDATION TRAP ESCAPE: injection {current_inject} → {new_inject} (flow r={r:.4f} < 0, hidden={hidden} flat at {crisis_ratio:.1f}x target, restoring routing)")
                                if not args.dry_run:
                                    state["injection_rate"] = new_inject
                                    adjust_injection_rate(new_inject)
                            elif r < 0.005:
                                new_inject = min(current_inject + 1, 4)
                                adjustments.append(f"DEEP CRISIS MIN-ROUTING: injection {current_inject} → {new_inject} (flow r={r:.4f} < 0.005, hidden={hidden} flat at {crisis_ratio:.1f}x target, restoring minimal routing)")
                                if not args.dry_run:
                                    state["injection_rate"] = new_inject
                                    adjust_injection_rate(new_inject)
                            else:
                                adjustments.append(f"Injection settled (stagnant): {current_inject} (at target, hidden={hidden} flat at {crisis_ratio:.1f}x target)")
                        else:
                            adjustments.append(f"Injection settled (deep crisis): {current_inject} (dead zone, hidden={hidden} {crisis_ratio:.1f}x target)")
                    elif current_inject < CRISIS_INJECTION_TARGET:
                        # Below target — push up
                        if any_stalemate_recently_perturbed(state, now):
                            adjustments.append(f"Injection perturbation window: {current_inject} (stalemate breaker recently fired, skipping consolidation)")
                        else:
                            new_inject = min(current_inject + 1, CRISIS_INJECTION_TARGET)
                            adjustments.append(f"DEEP CRISIS INJECTION PUSH: injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, below target)")
                            if not args.dry_run:
                                state["injection_rate"] = new_inject
                                adjust_injection_rate(new_inject)
                    else:
                        # Above target — consolidate DOWN toward target
                        if any_stalemate_recently_perturbed(state, now):
                            adjustments.append(f"Injection perturbation window: {current_inject} (stalemate breaker recently fired, skipping consolidation)")
                        elif r < 0:
                            # DEADLOCK FIX: injection above target with r<0 — consolidation
                            # would push DOWN but routing needs exploration. Push UP to
                            # restore routing capacity when flow is negative.
                            new_inject = min(current_inject + 1, 10)
                            adjustments.append(f"INJECTION TRAP ESCAPE: injection {current_inject} → {new_inject} (flow r={r:.4f} < 0, no-man's-land escape — restoring routing)")
                            if not args.dry_run:
                                state["injection_rate"] = new_inject
                                adjust_injection_rate(new_inject)
                        else:
                            new_inject = max(current_inject - 1, CRISIS_INJECTION_TARGET)
                            adjustments.append(f"DEEP CRISIS CONSOLIDATION: injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, consolidating)")
                            if not args.dry_run:
                                state["injection_rate"] = new_inject
                                adjust_injection_rate(new_inject)
                else:
                    # NORMAL CRISIS: injection pushes toward ceiling to surface hidden bridges.
                    # BUT: if consolidation is stagnant (hidden bridges flat for 5+ cycles),
                    # Check if consolidation is stagnant — if so, don't push injection back up
                    # (the deadlock breaker in the novelty section just consolidated it)
                    CRISIS_INJECT_CEILING = 15
                    deadlock_count = state.get('normal_crisis_deadlock_count', 0)
                    # DEEP DEADLOCK ESCAPE LOCK: when DEEP DEADLOCK ESCAPE just fired,
                    # respect its parameter reversal for the cooldown period.
                    deep_escape_until = state.get('deep_deadlock_escape_until', 0)
                    deep_escape_active = now < deep_escape_until
                    if deep_escape_active:
                        remaining = (deep_escape_until - now) / 60
                        adjustments.append(f"INJECTION LOCK (deep deadlock escape): injection {current_inject} (locked for {remaining:.1f}min, respecting strategy reversal)")
                    # DEADLOCK EXHAUSTED OVERRIDE: when deadlock breaker is exhausted (all params
                    # at targets but hidden bridges flat), push injection toward ceiling to flood
                    # the queue with experiments that route through hidden bridges.
                    if deep_escape_active:
                        # DEEP DEADLOCK ESCAPE LOCK: don't override the strategy reversal
                        pass
                    elif normal_crisis_stagnant and deadlock_count >= 2 and current_inject < CRISIS_INJECT_CEILING:
                        new_inject = min(current_inject + 1, CRISIS_INJECT_CEILING)
                        adjustments.append(f"CRISIS INJECTION PUSH (deadlock exhausted): injection {current_inject} → {new_inject} (hidden={hidden} flat {crisis_ratio:.1f}x target, all params at targets — flooding queue to break deadlock)")
                        if not args.dry_run:
                            state["injection_rate"] = new_inject
                            adjust_injection_rate(new_inject)
                    elif normal_crisis_stagnant and current_inject >= CRISIS_INJECTION_TARGET and current_inject <= CRISIS_INJECTION_TARGET + 1:
                        adjustments.append(f"CRISIS INJECTION HOLD (stagnant): injection {current_inject} (hidden={hidden} flat {crisis_ratio:.1f}x target, respecting consolidation from deadlock breaker)")
                    elif current_inject < CRISIS_INJECT_CEILING:
                        new_inject = min(current_inject + 1, CRISIS_INJECT_CEILING)
                        adjustments.append(f"CRISIS INJECTION PUSH: injection {current_inject} → {new_inject} (hidden={hidden} {crisis_ratio:.1f}x target, faster bridge surfacing)")
                        if not args.dry_run:
                            state["injection_rate"] = new_inject
                            adjust_injection_rate(new_inject)
                    else:
                        # Fix 12: injection at ceiling — HOLD instead of consolidating DOWN.
                        # The old deadlock breaker consolidated 15→12, undoing the exhausted
                        # override's push toward ceiling. Keep injection at ceiling to flood
                        # queue with experiments that route through hidden bridges.
                        adjustments.append(f"Injection at crisis ceiling: {current_inject} (hidden={hidden} {crisis_ratio:.1f}x target, at max — holding to flood queue)")
            elif hidden_growing:
                # Growing despite extreme params — push injection higher
                INJECTION_GROWING_CEILING = 10
                if current_inject < INJECTION_GROWING_CEILING:
                    new_inject = min(current_inject + 1, INJECTION_GROWING_CEILING)
                    adjustments.append(f"INJECTION BOOST (growing): injection {current_inject} → {new_inject} (hidden={hidden} growing, faster bridge surfacing)")
                    if not args.dry_run:
                        state["injection_rate"] = new_inject
                        adjust_injection_rate(new_inject)
                else:
                    adjustments.append(f"Injection at growing cap: {current_inject} (hidden={hidden} growing)")
            else:
                # Stable — moderate injection
                INJECTION_EXTREME_CEILING = 7
                if current_inject < INJECTION_EXTREME_CEILING:
                    new_inject = min(current_inject + 1, INJECTION_EXTREME_CEILING)
                    adjustments.append(f"INJECTION BOOST (extreme stable): injection {current_inject} → {new_inject} (hidden={hidden} stable)")
                    if not args.dry_run:
                        state["injection_rate"] = new_inject
                        adjust_injection_rate(new_inject)

        elif hidden > HIDDEN_BRIDGE_TARGET:
            # Above target but not extreme (15 < hidden < 45).
            # FIX (2026-06-20): Previous code had no handler for this range — 
            # injection only handled hidden < MIN and extreme_high (>=45).
            # This 15-45 dead zone meant 2.5x target hidden bridges received
            # zero injection response, allowing bridges to accumulate silently.
            if current_inject < INJECTION_RATE_MAX:
                new_inject = min(current_inject + 1, INJECTION_RATE_MAX)
                adjustments.append(f"Increase injection (above-target): {current_inject} → {new_inject} (hidden={hidden} {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target, surfacing hidden bridges)")
                if not args.dry_run:
                    state["injection_rate"] = new_inject
                    adjust_injection_rate(new_inject)
            else:
                adjustments.append(f"Injection at ceiling: {current_inject} (hidden={hidden} {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target, at max)")

    ctx.current_inject = current_inject


def _tune_trust_weight(ctx, args):
    """Phase 4 — trust_weight. Publishes to ctx: trust."""
    state = ctx.state
    adjustments = ctx.adjustments
    r = ctx.r
    outcome_r = ctx.outcome_r
    hidden = ctx.hidden
    routing_collapsed = ctx.routing_collapsed
    total_edges = ctx.total_edges
    zero_flow_edges = ctx.zero_flow_edges
    now = ctx.now
    extreme_high = ctx.extreme_high
    hidden_growing = ctx.hidden_growing
    hidden_history = ctx.hidden_history
    normal_crisis_stagnant = ctx.normal_crisis_stagnant

    # 4. Tune trust_weight based on hidden bridges and outcome-aware r
    trust = state.get("trust_weight", 0.15)
    if (hidden > 10 and outcome_r > 0.4) or extreme_high:
        if extreme_high:
            # Extreme deviation: hidden bridges 3x+ target
            # TREND-AWARE: When hidden bridges are growing, we need LESS trust
            # (spread traffic to discover hidden routes). Dead zone only applies
            # when hidden bridges are stable (consolidation working).
            # DEAD ZONE: Narrow for crisis — forces trust toward consolidation target
            TRUST_EXTREME_LOW = 0.10   # Narrowed from 0.08 — trust at 0.08 is below dead zone
            TRUST_EXTREME_HIGH = 0.13  # Narrowed from 0.15 — trust at 0.13 is at target
            crisis = hidden >= HIDDEN_BRIDGE_CRISIS
            # DEEP DEADLOCK ESCAPE LOCK: check if strategy reversal is locked
            deep_escape_until = state.get('deep_deadlock_escape_until', 0)
            deep_escape_active = now < deep_escape_until
            if deep_escape_active:
                remaining = (deep_escape_until - now) / 60
                adjustments.append(f"TRUST LOCK (deep deadlock escape): trust {trust:.2f} (locked for {remaining:.1f}min, respecting strategy reversal)")
            elif crisis and hidden_growing:
                # CRISIS + GROWING: adjust trust based on crisis severity.
                # At 10x+ target (deep crisis), all params at extremes = saturation loop.
                # Consolidate trust toward target to break the feedback.
                crisis_ratio = hidden / HIDDEN_BRIDGE_TARGET
                if crisis_ratio >= 10:
                    # DEEP CRISIS + GROWING: consolidate trust toward target
                    if trust < CRISIS_TRUST_TARGET:
                        new_trust = min(trust + 0.02, CRISIS_TRUST_TARGET)
                        adjustments.append(f"DEEP CRISIS CONSOLIDATION (growing): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, breaking saturation)")
                        if not args.dry_run:
                            state["trust_weight"] = new_trust
                    elif trust > CRISIS_TRUST_TARGET:
                        # FIX: When trust is far above target, high trust
                        # concentrates traffic on known routes, PREVENTING
                        # discovery of hidden bridges. Always consolidate
                        # trust downward in deep crisis, even if flow r < 0.
                        # The negative flow r is partly caused by high trust
                        # blocking exploration of hidden routes.
                        new_trust = max(trust - 0.02, CRISIS_TRUST_TARGET)
                        adjustments.append(f"DEEP CRISIS REDUCTION (growing): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, breaking saturation)")
                        if not args.dry_run:
                            state["trust_weight"] = new_trust
                    else:
                        adjustments.append(f"Trust at consolidation target: {trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, deep crisis)")
                else:
                    # NORMAL CRISIS + GROWING: push trust DOWN to spread traffic
                    CRISIS_TRUST_FLOOR = 0.05
                    if trust > CRISIS_TRUST_FLOOR:
                        new_trust = max(trust - 0.03, CRISIS_TRUST_FLOOR)
                        adjustments.append(f"CRISIS TRUST REDUCTION (growing): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, spread traffic)")
                        if not args.dry_run:
                            state["trust_weight"] = new_trust
                    else:
                        adjustments.append(f"Trust at crisis explore floor: {trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, growing)")
            elif crisis:
                # CRISIS + STABLE: Only consolidate if trust is at exploration extreme.
                # If trust is at mid-range, push trust reduction to spread traffic.
                TRUST_AT_EXTREME = 0.08
                # Dead zone width scales inversely with crisis severity:
                # - 5x target (75): wide dead zone [0.08, 0.15] (prevent oscillation)
                # - 10x target (150): narrow dead zone [0.08, 0.12] (push exploration)
                # - 15x+ target (225+): bypass dead zone entirely (extreme crisis)
                crisis_ratio = hidden / HIDDEN_BRIDGE_TARGET
                if crisis_ratio >= 15:
                    # EXTREME CRISIS: bypass dead zone, consolidate toward target
                    # STALEMATE BREAKER: DISABLED at extreme crisis (>=15x target)
                    # System needs hard consolidation, not exploration perturbation.
                    # STAGNANT DETECTION: If hidden bridges flat for 5+ cycles at target,
                    # break dead zone with small perturbation.
                    extreme_stagnant = False
                    if len(hidden_history) >= 5:
                        recent5 = hidden_history[-5:]
                        h_range = max(recent5) - min(recent5)
                        h_min = min(recent5)
                        h_pct_range = h_range / h_min if h_min > 0 else 999
                        if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15:
                            extreme_stagnant = True

                    STAGNANT_COOLDOWN_TRUST = 120  # 2 minutes (reduced for crisis responsiveness)
                    last_stagnant_trust = state.get("last_stagnant_time_trust", 0)
                    stagnant_trust_cooldown = now - last_stagnant_trust < STAGNANT_COOLDOWN_TRUST

                    # Stagnant trust target aligned with crisis target
                    STAGNANT_EXTREME_TRUST = 0.05  # Aligned with CRISIS_TRUST_TARGET
                    if extreme_stagnant and not stagnant_trust_cooldown:
                        if trust < STAGNANT_EXTREME_TRUST - 0.001:
                            new_trust = min(trust + 0.01, STAGNANT_EXTREME_TRUST)
                            adjustments.append(f"EXTREME STAGNANT PERTURB: trust {trust:.2f} \u2192 {new_trust:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, breaking dead zone)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                                state["last_stagnant_time_trust"] = now
                        elif trust > STAGNANT_EXTREME_TRUST:
                            # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                            escape_threshold_trust = -0.05 if crisis_ratio >= 5 else 0
                            if r < escape_threshold_trust:
                                # Flow deeply negative — consolidation has killed routing.
                                # Push trust UP to escape consolidation trap.
                                # Cap at 0.15 (mid-range) not 0.08 — at 0.08 trust is too low for routing.
                                new_trust = min(trust + 0.02, 0.15)
                                adjustments.append(f"CONSOLIDATION TRAP ESCAPE: trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0, pushing toward exploration to restore routing)")
                                if not args.dry_run:
                                    state["trust_weight"] = new_trust
                                    state["last_stagnant_time_trust"] = now
                            elif r < 0.01:
                                # Flow critically low but not negative.
                                # CRISIS OVERRIDE: When hidden bridges flat at 18x+ target,
                                # consolidation is MORE important than routing capacity.
                                # STAGNANT CRISIS ESCAPE: If hidden bridges flat for 5+ cycles,
                                # consolidation has FAILED. Push UP to try exploration.
                                if crisis_ratio >= 18 and trust > STAGNANT_EXTREME_TRUST + 0.02:
                                    consolidation_stuck = False
                                    if len(hidden_history) >= 5:
                                        recent5 = hidden_history[-5:]
                                        h_range = max(recent5) - min(recent5)
                                        h_min = min(recent5)
                                        h_pct_range = h_range / h_min if h_min > 0 else 999
                                        if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15:
                                            consolidation_stuck = True
                                    if consolidation_stuck:
                                        # FIX 2026-06-15: Cap was 0.12 → no-op. Raised to 0.20 → still no-op at current=0.20.
                                        # FIX 2026-06-15-v2: Cap now 0.25 (83% of max 0.30) to allow genuine increase.
                                        new_trust = min(trust + 0.03, 0.25)
                                        if new_trust <= trust:
                                            # DEADLOCK BREAKER: Trust can't go higher (already at cap).
                                            # Flip to hard consolidation — force trust DOWN toward crisis target.
                                            new_trust = max(trust - 0.05, CRISIS_TRUST_TARGET)
                                            adjustments.append(f"DEADLOCK BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, exploration maxed & consolidation failed, forcing hard consolidation)")
                                        else:
                                            adjustments.append(f"STAGNANT CRISIS ESCAPE (consolidation failed): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat for 5+ cycles, consolidation not working, trying exploration)")
                                        if not args.dry_run:
                                            state["trust_weight"] = new_trust
                                            state["last_stagnant_time_trust"] = now
                                    else:
                                        new_trust = max(trust - 0.01, STAGNANT_EXTREME_TRUST)
                                        adjustments.append(f"EXTREME CRISIS CONSOLIDATION (flow override): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat, consolidating despite low flow)")
                                        if not args.dry_run:
                                            state["trust_weight"] = new_trust
                                            state["last_stagnant_time_trust"] = now
                                elif trust <= STAGNANT_EXTREME_TRUST + 0.06:
                                    # CRISIS GUARD: At 18x+ target, consolidation overrides routing
                                    if crisis_ratio >= 18:
                                        # ROUTING COLLAPSE ESCAPE: When ALL flow is zero, consolidation
                                        # has killed routing entirely. Push params UP to restore routing.
                                        if routing_collapsed:
                                            new_trust = min(trust + 0.02, 0.10)
                                            adjustments.append(f"ROUTING COLLAPSE ESCAPE: trust {trust:.2f} → {new_trust:.2f} (routing dead — {zero_flow_edges}/{total_edges} edges zero flow, restoring routing to break deadlock)")
                                            if not args.dry_run:
                                                state["trust_weight"] = new_trust
                                                state["last_stagnant_time_trust"] = now
                                        else:
                                            adjustments.append(f"EXTREME CRISIS HOLD (consolidation priority): trust {trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, consolidation overrides routing)")
                                    else:
                                        new_trust = min(trust + 0.03, 0.12)
                                        adjustments.append(f"EXTREME STAGNANT MIN-ROUTING (near target): trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0.01, restoring minimal routing)")
                                        if not args.dry_run:
                                            state["trust_weight"] = new_trust
                                            state["last_stagnant_time_trust"] = now
                                else:
                                    adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): trust {trust:.2f} (flow r={r:.4f} < 0.005, holding above target to preserve routing)")
                            else:
                                # Only PERTURB DOWN when flow r is healthy enough.
                                # At r < 0.01, system still needs more routing capacity.
                                if r >= 0.01:
                                    new_trust = max(trust - 0.01, STAGNANT_EXTREME_TRUST)
                                    adjustments.append(f"EXTREME STAGNANT PERTURB: trust {trust:.2f} \u2192 {new_trust:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target for 5+ cycles, breaking dead zone)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                        state["last_stagnant_time_trust"] = now
                                else:
                                    adjustments.append(f"EXTREME STAGNANT HOLD (flow low): trust {trust:.2f} (flow r={r:.4f} < 0.01, preserving routing capacity)")
                        else:
                            # At stagnant target — check for minimum viable routing
                            # When flow r is critically low, system is paralyzed. Push trust
                            # slightly above stagnant target to restore minimal routing confidence.
                            if r < 0.01:
                                # CRISIS GUARD: At 18x+ target, consolidation overrides routing
                                if crisis_ratio >= 18:
                                    # ROUTING COLLAPSE ESCAPE: When ALL flow is zero, consolidation
                                    # has killed routing entirely. Push params UP to restore routing.
                                    if routing_collapsed:
                                        new_trust = min(trust + 0.02, 0.10)
                                        adjustments.append(f"ROUTING COLLAPSE ESCAPE: trust {trust:.2f} → {new_trust:.2f} (routing dead — {zero_flow_edges}/{total_edges} edges zero flow, restoring routing to break deadlock)")
                                        if not args.dry_run:
                                            state["trust_weight"] = new_trust
                                            state["last_stagnant_time_trust"] = now
                                    else:
                                        # DEADLOCK FIX: At stagnant target with flat hidden bridges,
                                        # consolidation has FAILED. Force exploration to break local minimum.
                                        if extreme_stagnant:
                                            new_trust = min(trust + 0.03, 0.30)
                                            if new_trust <= trust:
                                                adjustments.append(f"TRUST DEADLOCK: trust {trust:.2f} (at stagnant target, exploration maxed, holding)")
                                            else:
                                                adjustments.append(f"STAGNANT TARGET ESCAPE (consolidation failed): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat for 5+ cycles, at floor — forcing exploration)")
                                                if not args.dry_run:
                                                    state["trust_weight"] = new_trust
                                                    state["last_stagnant_time_trust"] = now
                                        else:
                                            adjustments.append(f"EXTREME CRISIS HOLD (consolidation priority): trust {trust:.2f} (at stagnant target, consolidation overrides routing)")
                                else:
                                    new_trust = min(trust + 0.02, 0.08)
                                    adjustments.append(f"EXTREME STAGNANT MIN-ROUTING: trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0.01, restoring minimal routing)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                        state["last_stagnant_time_trust"] = now
                            else:
                                # HOLD (don't push further, prevents bounce-back oscillation)
                                adjustments.append(f"EXTREME STAGNANT HOLD: trust {trust:.2f} (at stagnant target, holding)")
                    elif extreme_stagnant and stagnant_trust_cooldown:
                        # Crisis bypass: when flow r critically low, bypass stagnant cooldown
                        if r < 0.01:
                            adjustments.append(f"CRISIS BYPASS: trust stagnant cooldown bypassed (flow r={r:.4f} < 0.01, extreme stagnant)")
                            # FIX: Flow r < 0 means consolidation has KILLED routing.
                            # Pushing DOWN further makes it worse. ALWAYS escape first.
                            if trust > STAGNANT_EXTREME_TRUST:
                                # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                                escape_threshold_trust_bypass = -0.05 if crisis_ratio >= 5 else 0
                                if r < escape_threshold_trust_bypass:
                                    # Flow deeply negative — consolidation trap. Push UP.
                                    new_trust = min(trust + 0.01, 0.08)
                                    adjustments.append(f"CONSOLIDATION TRAP ESCAPE (bypass): trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0, pushing toward exploration)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                        state["last_stagnant_time_trust"] = now
                                elif routing_collapsed:
                                    # ROUTING COLLAPSE: all flow zero — push UP to restore routing
                                    new_trust = min(trust + 0.02, 0.10)
                                    adjustments.append(f"ROUTING COLLAPSE ESCAPE (bypass): trust {trust:.2f} → {new_trust:.2f} (routing dead, restoring routing to break deadlock)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                        state["last_stagnant_time_trust"] = now
                                else:
                                    # DEADLOCK BREAKER (bypass): params far above consolidation targets
                                    if hidden_history and len(hidden_history) >= 5:
                                        recent5 = hidden_history[-5:]
                                        h_range = max(recent5) - min(recent5)
                                        h_min = min(recent5)
                                        h_pct_range = h_range / h_min if h_min > 0 else 999
                                        if h_pct_range < 0.015 and h_min >= HIDDEN_BRIDGE_TARGET * 15 and trust >= STAGNANT_EXTREME_TRUST + 0.10:
                                            new_trust = max(trust - 0.03, STAGNANT_EXTREME_TRUST)
                                            adjustments.append(f"DEADLOCK BREAKER (bypass): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target flat 5+ cycles, params far above target, forcing consolidation)")
                                            if not args.dry_run:
                                                state["trust_weight"] = new_trust
                                                state["last_stagnant_time_trust"] = now
                                        else:
                                            adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): trust {trust:.2f} (flow r critically low, holding above target to preserve routing)")
                                    else:
                                        adjustments.append(f"EXTREME STAGNANT HOLD (flow crisis): trust {trust:.2f} (flow r critically low, holding above target to preserve routing)")
                            elif trust < STAGNANT_EXTREME_TRUST - 0.001:
                                new_trust = min(trust + 0.02, STAGNANT_EXTREME_TRUST)
                                adjustments.append(f"EXTREME STAGNANT PERTURB (crisis): trust {trust:.2f} → {new_trust:.2f} (flow r critically low, bypassing cooldown)")
                                if not args.dry_run:
                                    state["trust_weight"] = new_trust
                                    state["last_stagnant_time_trust"] = now
                            else:
                                adjustments.append(f"EXTREME STAGNANT HOLD: trust {trust:.2f} (at stagnant target, holding)")
                        else:
                            adjustments.append(f"Stagnant perturbation cooldown (trust): {STAGNANT_COOLDOWN_TRUST - (now - last_stagnant_trust):.0f}s remaining (avoiding oscillation)")
                    elif trust < CRISIS_TRUST_TARGET:
                        new_trust = min(trust + 0.02, CRISIS_TRUST_TARGET)
                        adjustments.append(f"EXTREME CRISIS CONSOLIDATION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, bypass dead zone, consolidating)")
                        if not args.dry_run:
                            state["trust_weight"] = new_trust
                    elif trust > CRISIS_TRUST_TARGET:
                        new_trust = max(trust - 0.02, CRISIS_TRUST_TARGET)
                        adjustments.append(f"EXTREME CRISIS REDUCTION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, bypass dead zone, reducing)")
                        if not args.dry_run:
                            state["trust_weight"] = new_trust
                    else:
                        adjustments.append(f"Trust at consolidation target: {trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target)")
                elif crisis_ratio >= 10:
                    # DEEP CRISIS: narrow dead zone, consolidate toward target
                    TRUST_CONSOLIDATE_LOW = 0.10
                    TRUST_CONSOLIDATE_HIGH = 0.14
                    # STALEMATE BREAKER: enabled at deep crisis (prevents dead zone equilibrium)
                    stalemate = False  # DISABLED at deep crisis - stalemate breaker pushes exploration when consolidation needed
                    if False and len(hidden_history) >= 3:  # DISABLED - see auto-tune-stalemate-breaker-fix skill
                        h3 = hidden_history[-3:]
                        if max(h3) - min(h3) <= 5 and min(h3) >= HIDDEN_BRIDGE_TARGET * 10:
                            # Stable bridges at 10x+ target = consolidation failing, need perturbation
                            stalemate = True
                    if stalemate:
                        STALEMATE_COOLDOWN = 360  # 6 minutes
                        last_stalemate_trust = state.get("last_stalemate_time_trust", 0)
                        if now - last_stalemate_trust < STALEMATE_COOLDOWN:
                            adjustments.append(f"Stalemate breaker cooldown (trust): {STALEMATE_COOLDOWN - (now - last_stalemate_trust):.0f}s remaining")
                        else:
                            # System stuck — alternate trust direction to break deadlock
                            stalemate_count = len([h for h in hidden_history[-3:] if h == hidden_history[-1]])
                            TRUST_STALEMATE_LOW = 0.07  # Below dead zone [0.10, 0.14]
                            TRUST_STALEMATE_HIGH = 0.17  # Above dead zone [0.10, 0.14]
                            if trust > TRUST_CONSOLIDATE_HIGH:
                                # Already above dead zone — alternate direction
                                if trust < TRUST_STALEMATE_HIGH:
                                    new_trust = min(trust + 0.02, TRUST_STALEMATE_HIGH)
                                    adjustments.append(f"STALEMATE BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, pushing above dead zone)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                elif trust > TRUST_STALEMATE_LOW:
                                    # At ceiling — push DOWN on alternating cycles to break stalemate
                                    new_trust = max(trust - 0.04, TRUST_STALEMATE_LOW)
                                    adjustments.append(f"STALEMATE BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, at ceiling — reversing toward floor)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                else:
                                    adjustments.append(f"Trust at stalemate ceiling: {trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, above dead zone)")
                            elif trust < TRUST_CONSOLIDATE_LOW:
                                # Already below dead zone — alternate direction
                                if trust > TRUST_STALEMATE_LOW:
                                    new_trust = max(trust - 0.02, TRUST_STALEMATE_LOW)
                                    adjustments.append(f"STALEMATE BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, pushing below dead zone)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                elif trust < TRUST_STALEMATE_HIGH:
                                    # At floor — push UP on alternating cycles to break stalemate
                                    new_trust = min(trust + 0.04, TRUST_STALEMATE_HIGH)
                                    adjustments.append(f"STALEMATE BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, at floor — reversing toward ceiling)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                else:
                                    adjustments.append(f"Trust at stalemate floor: {trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, below dead zone)")
                            else:
                                # Inside dead zone — alternate direction based on cycle count
                                if stalemate_count % 2 == 0:
                                    # Even cycle: push DOWN below dead zone
                                    new_trust = max(trust - 0.04, TRUST_STALEMATE_LOW)
                                    adjustments.append(f"STALEMATE BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, pushing below dead zone)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                else:
                                    # Odd cycle: push UP above dead zone
                                    new_trust = min(trust + 0.03, TRUST_STALEMATE_HIGH)
                                    adjustments.append(f"STALEMATE BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable at {crisis_ratio:.1f}x target, pushing above dead zone)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                            # Update last_stalemate_time_trust after any stalemate adjustment
                            if not args.dry_run:
                                state["last_stalemate_time_trust"] = now
                    elif TRUST_CONSOLIDATE_LOW <= trust <= TRUST_CONSOLIDATE_HIGH:
                        if any_stalemate_recently_perturbed(state, now):
                            adjustments.append(f"Trust perturbation window: {trust:.2f} (stalemate breaker recently fired, skipping consolidation)")
                        elif is_consolidation_stagnant(hidden_history, hidden):
                            # STAGNANT: hidden bridges flat at deep crisis for 3+ cycles.
                            # Tighten dead zone to force trust toward consolidation target (0.12).
                            STAGNANT_TRUST_LOW = 0.11
                            STAGNANT_TRUST_HIGH = 0.13
                            STAGNANT_TRUST_TARGET = 0.12  # Must match CRISIS_TRUST_TARGET
                            if trust > STAGNANT_TRUST_HIGH:
                                if r < 0:
                                    # DEADLOCK FIX: trust above target with r<0 — consolidation
                                    # would push DOWN but routing needs exploration. Push UP
                                    # to restore routing when flow is negative.
                                    new_trust = min(trust + 0.02, 0.25)
                                    adjustments.append(f"STAGNANT TRAP ESCAPE: trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0, no-man's-land escape — restoring routing)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                else:
                                    new_trust = max(trust - 0.02, STAGNANT_TRUST_TARGET)
                                    adjustments.append(f"STAGNANT CONSOLIDATION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, tightening dead zone)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                            elif trust < STAGNANT_TRUST_LOW:
                                new_trust = min(trust + 0.02, STAGNANT_TRUST_LOW)
                                adjustments.append(f"STAGNANT CONSOLIDATION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, tightening dead zone)")
                                if not args.dry_run:
                                    state["trust_weight"] = new_trust
                            else:
                                # In dead zone but not at target — push toward target
                                if trust > STAGNANT_TRUST_TARGET:
                                    new_trust = max(trust - 0.01, STAGNANT_TRUST_TARGET)
                                    adjustments.append(f"STAGNANT CONSOLIDATION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, pushing toward target 0.12)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                elif trust < STAGNANT_TRUST_TARGET:
                                    new_trust = min(trust + 0.01, STAGNANT_TRUST_TARGET)
                                    adjustments.append(f"STAGNANT CONSOLIDATION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat at {crisis_ratio:.1f}x target, pushing toward target 0.12)")
                                    if not args.dry_run:
                                        state["trust_weight"] = new_trust
                                else:
                                    # At stagnant target but flow is dead — consolidation trap
                                    # Crisis guard: at 5x+ hidden bridges, only escape when r is truly negative
                                    escape_threshold_deep_trust = -0.05 if crisis_ratio >= 5 else 0
                                    if r < escape_threshold_deep_trust:
                                        new_trust = min(trust + 0.03, 0.20)
                                        adjustments.append(f"DEEP CRISIS CONSOLIDATION TRAP ESCAPE: trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0, hidden={hidden} flat at {crisis_ratio:.1f}x target, restoring routing)")
                                        if not args.dry_run:
                                            state["trust_weight"] = new_trust
                                    elif r < 0.005:
                                        new_trust = min(trust + 0.01, 0.14)
                                        adjustments.append(f"DEEP CRISIS MIN-ROUTING: trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0.005, hidden={hidden} flat at {crisis_ratio:.1f}x target, restoring minimal routing)")
                                        if not args.dry_run:
                                            state["trust_weight"] = new_trust
                                    else:
                                        adjustments.append(f"Trust settled (stagnant): {trust:.2f} (at target 0.12, hidden={hidden} flat at {crisis_ratio:.1f}x target)")
                        else:
                            adjustments.append(f"Trust settled (deep crisis): {trust:.2f} (dead zone [{TRUST_CONSOLIDATE_LOW}, {TRUST_CONSOLIDATE_HIGH}], hidden={hidden} {crisis_ratio:.1f}x target)")
                    elif trust < TRUST_CONSOLIDATE_LOW:
                        # Below consolidation zone — push UP toward target
                        if any_stalemate_recently_perturbed(state, now):
                            adjustments.append(f"Trust perturbation window: {trust:.2f} (stalemate breaker recently fired, skipping consolidation)")
                        else:
                            new_trust = min(trust + 0.02, TRUST_CONSOLIDATE_LOW)
                            adjustments.append(f"DEEP CRISIS CONSOLIDATION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, pushing toward consolidation)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                    else:
                        # Above consolidation zone — push DOWN toward target
                        if r < 0:
                            # DEADLOCK FIX: trust above consolidation zone with r<0 —
                            # consolidation would push DOWN but routing needs exploration.
                            # Push UP to restore routing when flow is negative.
                            new_trust = min(trust + 0.02, 0.30)
                            adjustments.append(f"TRUST TRAP ESCAPE: trust {trust:.2f} → {new_trust:.2f} (flow r={r:.4f} < 0, no-man's-land escape — restoring routing)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                        elif any_stalemate_recently_perturbed(state, now):
                            adjustments.append(f"Trust perturbation window: {trust:.2f} (stalemate breaker recently fired, skipping consolidation)")
                        else:
                            new_trust = max(trust - 0.02, TRUST_CONSOLIDATE_HIGH)
                            adjustments.append(f"DEEP CRISIS REDUCTION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} {crisis_ratio:.1f}x target, reducing)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                else:
                    # NORMAL CRISIS: original wide dead zone
                    if TRUST_EXTREME_LOW <= trust <= TRUST_EXTREME_HIGH:
                        # DEADLOCK BREAKER: trust in dead zone but hidden bridges flat
                        deadlock_count = state.get('normal_crisis_deadlock_count', 0)
                        state['normal_crisis_deadlock_count'] = deadlock_count + 1

                        CRISIS_TRUST_FLOOR = 0.05
                        # Fix 12: When deadlock_count >= 2, push trust DOWN (matching
                        # the exhausted override behavior) instead of UP. The old logic
                        # pushed trust UP toward target, which UNDID the exhausted
                        # override's push DOWN, creating oscillation 0.11↔0.14.
                        if deadlock_count >= 2 and trust > CRISIS_TRUST_FLOOR:
                            new_trust = max(trust - 0.03, CRISIS_TRUST_FLOOR)
                            adjustments.append(f"TRUST DEADLOCK BREAKER (exhausted): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target for {deadlock_count+1} cycles, spreading traffic to break deadlock)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                        elif deadlock_count >= 2 and trust < CRISIS_TRUST_TARGET:
                            new_trust = min(trust + 0.03, CRISIS_TRUST_TARGET)
                            adjustments.append(f"TRUST DEADLOCK BREAKER: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target for {deadlock_count+1} cycles, forcing toward target)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                                state['normal_crisis_deadlock_count'] = 0
                        else:
                            adjustments.append(f"Trust settled: {trust:.2f} (crisis dead zone, hidden={hidden} stable, deadlock_count={deadlock_count+1})")
                    elif trust <= TRUST_AT_EXTREME:
                        CRISIS_TRUST_FLOOR = 0.05
                        # Trust at extreme — consolidate to break saturation
                        # BUT: when hidden bridges are flat at crisis level,
                        # consolidating trust UP makes things worse — we need
                        # trust at the FLOOR to spread traffic and surface
                        # hidden bridges. Hold instead of pushing UP.
                        if normal_crisis_stagnant and trust >= CRISIS_TRUST_FLOOR and trust > CRISIS_TRUST_FLOOR:
                            new_trust = max(trust - 0.02, CRISIS_TRUST_FLOOR)
                            adjustments.append(f"CRISIS CONSOLIDATION (stagnant override): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target, trust below dead zone but hidden bridges flat — spreading traffic)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                        elif normal_crisis_stagnant and trust <= CRISIS_TRUST_FLOOR + 0.01:
                            adjustments.append(f"CRISIS TRUST HOLD (at floor, spreading): trust {trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target, trust at floor — holding to spread traffic)")
                        elif trust < CRISIS_TRUST_TARGET:
                            new_trust = min(trust + 0.02, CRISIS_TRUST_TARGET)
                            adjustments.append(f"CRISIS CONSOLIDATION: trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} crisis, params at extreme — consolidation)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                    else:
                        # Trust NOT at extreme — push trust reduction even in stable crisis
                        # BUT: if consolidation is stagnant (hidden bridges flat for 5+ cycles),
                        # the deadlock breaker in the novelty section may have just consolidated
                        # trust UP. Don't immediately reverse that — respect the consolidation.
                        CRISIS_TRUST_FLOOR = 0.05
                        deadlock_count = state.get('normal_crisis_deadlock_count', 0)
                        # DEADLOCK EXHAUSTED OVERRIDE: when deadlock breaker is exhausted (all params
                        # at targets but hidden bridges flat), push trust toward floor to spread
                        # traffic across more domains and surface hidden bridges.
                        if normal_crisis_stagnant and deadlock_count >= 2 and trust > CRISIS_TRUST_FLOOR:
                            new_trust = max(trust - 0.03, CRISIS_TRUST_FLOOR)
                            adjustments.append(f"CRISIS TRUST REDUCTION (deadlock exhausted): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target, all params at targets — spreading traffic to break deadlock)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
                        elif normal_crisis_stagnant and trust <= CRISIS_TRUST_TARGET + 0.03:
                            adjustments.append(f"CRISIS TRUST HOLD (stagnant): trust {trust:.2f} (hidden={hidden} flat {crisis_ratio:.1f}x target, respecting consolidation from deadlock breaker)")
                        elif trust > CRISIS_TRUST_FLOOR:
                            new_trust = max(trust - 0.03, CRISIS_TRUST_FLOOR)
                            adjustments.append(f"CRISIS TRUST REDUCTION (stable): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} crisis, params mid-range — spread traffic)")
                            if not args.dry_run:
                                state["trust_weight"] = new_trust
            elif hidden_growing:
                # Hidden bridges GROWING → reduce trust to spread traffic
                if trust > 0.05:
                    new_trust = max(trust - 0.03, 0.05)
                    adjustments.append(f"TRUST REDUCTION (growing): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} growing, spread traffic)")
                    if not args.dry_run:
                        state["trust_weight"] = new_trust
                else:
                    adjustments.append(f"Trust at exploration floor: {trust:.2f} (hidden={hidden} growing)")
            elif trust < TRUST_EXTREME_LOW:
                # When hidden bridges are elevated and stable, keep trust LOW to spread traffic
                if hidden >= HIDDEN_EXTREME_HIGH and len(hidden_history) >= 5:
                    recent5 = hidden_history[-5:]
                    h_range = max(recent5) - min(recent5)
                    if h_range <= 3:
                        # Hidden bridges stable at 3x+ target — hold trust low to spread traffic
                        adjustments.append(f"Trust HOLD (elevated stable): trust {trust:.2f} (hidden={hidden} stable {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target, keeping low to spread traffic)")
                    else:
                        new_trust = min(trust + 0.03, TRUST_EXTREME_LOW)
                        adjustments.append(f"CONSOLIDATION (extreme): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable — below dead zone)")
                        if not args.dry_run:
                            state["trust_weight"] = new_trust
                else:
                    new_trust = min(trust + 0.03, TRUST_EXTREME_LOW)
                    adjustments.append(f"CONSOLIDATION (extreme): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable — below dead zone)")
                    if not args.dry_run:
                        state["trust_weight"] = new_trust
            elif trust > TRUST_EXTREME_HIGH:
                new_trust = max(trust - 0.03, TRUST_EXTREME_HIGH)
                adjustments.append(f"CONSOLIDATION (extreme): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} stable — above dead zone)")
                if not args.dry_run:
                    state["trust_weight"] = new_trust
            else:
                # In dead zone AND stable — no change
                adjustments.append(f"Trust settled: {trust:.2f} (extreme_high dead zone, hidden={hidden} stable)")
        elif TRUST_SETTLED_LOW <= trust <= TRUST_SETTLED_HIGH:
            # In settled range and NOT extreme — no adjustment needed
            # BUT: when hidden bridges are elevated (>=20, 1.3x target) AND flat
            # for 5+ cycles, trust in this dead zone prevents traffic from
            # reaching hidden bridges. Push trust BELOW the settled range to
            # spread traffic and surface hidden bridges.
            # (Fix 24: 2026-06-21 — trust already in dead zone during elevated stagnation)
            if hidden >= 20 and len(hidden_history) >= 5:
                recent5 = hidden_history[-5:]
                h_range = max(recent5) - min(recent5)
                if h_range <= 3 and trust >= TRUST_SETTLED_LOW:
                    new_trust = max(trust - 0.04, TRUST_SETTLED_LOW - 0.03)
                    adjustments.append(f"Trust REDUCTION (elevated stagnant dead zone): trust {trust:.2f} → {new_trust:.2f} (hidden={hidden} flat {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target for 5+ cycles, trust in dead zone — pushing below to spread traffic to hidden bridges)")
                    if not args.dry_run:
                        state["trust_weight"] = new_trust
                else:
                    pass  # Normal settled — no change
            else:
                pass  # Normal settled — no change
        elif trust < TRUST_SETTLED_LOW:
            # Below settled range — increase toward it
            # BUT: when hidden bridges are elevated (>=20, 1.3x target) AND flat
            # for 5+ cycles, pushing trust into the settled dead zone [0.15-0.25]
            # creates a deadlock where no parameter can move. Keep trust below the
            # settled range to maintain traffic spread toward hidden bridges.
            # (Fix 23: 2026-06-20 — prevents trust dead-zone trap at 2-3x target)
            if hidden >= 20 and len(hidden_history) >= 5:
                recent5 = hidden_history[-5:]
                h_range = max(recent5) - min(recent5)
                if h_range <= 3 and trust <= 0.14:
                    # Hidden bridges flat at 1.3-3x target — don't push trust
                    # into settled dead zone. Hold below to spread traffic.
                    adjustments.append(f"Trust HOLD (elevated stagnant): trust {trust:.2f} (hidden={hidden} flat {hidden/HIDDEN_BRIDGE_TARGET:.1f}x target for 5+ cycles, keeping below settled range to spread traffic to hidden bridges)")
                    # Don't change trust — skip the increase
                else:
                    new_trust = min(trust + 0.02, TRUST_SETTLED_HIGH)
                    adjustments.append(f"Increase trust_weight: {trust:.2f} → {new_trust:.2f} (hidden={hidden}, outcome_r={outcome_r:.2f})")
                    if not args.dry_run:
                        state["trust_weight"] = new_trust
            else:
                new_trust = min(trust + 0.02, TRUST_SETTLED_HIGH)
                adjustments.append(f"Increase trust_weight: {trust:.2f} → {new_trust:.2f} (hidden={hidden}, outcome_r={outcome_r:.2f})")
                if not args.dry_run:
                    state["trust_weight"] = new_trust
        elif trust > TRUST_SETTLED_HIGH:
            # Above settled range — decrease toward it
            new_trust = max(trust - 0.02, TRUST_SETTLED_LOW)
            adjustments.append(f"Decrease trust_weight: {trust:.2f} → {new_trust:.2f} (hidden={hidden}, outcome_r={outcome_r:.2f})")
            if not args.dry_run:
                state["trust_weight"] = new_trust
    elif hidden < 5:
        # Few hidden bridges → need more exploration, reduce trust
        # hidden=0 is ALWAYS critical — decrease trust to encourage exploration
        if hidden == 0:
            # When flow r is negative, trust provides routing stability.
            # Don't push trust down further — it would destabilize routing
            # when the system is already struggling. HOLD instead.
            if r < 0:
                adjustments.append(f"Trust HOLD (hidden=0, flow r={r:.4f} < 0): keeping trust at {trust:.2f} to preserve routing stability")
            else:
                new_trust = max(trust - 0.02, TRUST_WEIGHT_MIN)
                if new_trust != trust:
                    adjustments.append(f"Decrease trust_weight: {trust:.2f} → {new_trust:.2f} (hidden=0, flow r={r:.4f})")
                    if not args.dry_run:
                        state["trust_weight"] = new_trust
        elif outcome_r >= 0.80:
            # Low but non-zero hidden bridges with healthy outcome_r — monitoring
            adjustments.append(f"Hidden bridges low ({hidden}) but outcome_r={outcome_r:.3f} — monitoring, no trust decrease")
        else:
            new_trust = max(trust - 0.02, TRUST_WEIGHT_MIN)
            if new_trust != trust:
                adjustments.append(f"Decrease trust_weight: {trust:.2f} → {new_trust:.2f} (low bridges, need exploration)")
                if not args.dry_run:
                    state["trust_weight"] = new_trust

    ctx.trust = trust


def _self_change_guard(ctx):
    """Manual-override detection (self-change guard). Mutates state in place."""
    state = ctx.state
    adjustments = ctx.adjustments
    now = ctx.now
    novelty = ctx.novelty
    trust = ctx.trust
    current_inject = ctx.current_inject

    # Record manual override time if parameters changed significantly from last cycle
    # BUT ONLY if the change was NOT made by the auto-tune itself this cycle.
    # The auto-tune can push novelty/injection/trust by large amounts during
    # crisis mode — these are not manual overrides and must not trigger cooldown.
    last_novelty = state.get('_prev_novelty', novelty)
    last_trust = state.get('_prev_trust', trust)
    last_injection = state.get('_prev_injection', current_inject)

    # Check if any auto-tune adjustment in this cycle touched each parameter.
    # Use broad matching: any mention of the parameter name means auto-tune
    # processed it (even STAGNANT HOLD which says "novelty 0.30" without →).
    auto_changed_novelty = any("novelty" in a.lower() for a in adjustments)
    auto_changed_trust = any("trust" in a.lower() for a in adjustments)
    auto_changed_injection = any("injection" in a.lower() for a in adjustments)

    novelty_delta = abs(novelty - last_novelty) > 0.03 and not auto_changed_novelty
    trust_delta = abs(trust - last_trust) > 0.02 and not auto_changed_trust
    injection_delta = abs(current_inject - last_injection) > 2 and not auto_changed_injection

    if novelty_delta or trust_delta or injection_delta:
        state['last_manual_override_time'] = now
        adjustments.append(f"MANUAL OVERRIDE DETECTED: novelty {last_novelty:.2f}→{novelty:.2f}, trust {last_trust:.2f}→{trust:.2f}, injection {last_injection}→{current_inject} — recording override time for cooldown")

    # Save previous values for next cycle comparison
    state['_prev_novelty'] = novelty
    state['_prev_trust'] = trust
    state['_prev_injection'] = current_inject


def main():
    parser = argparse.ArgumentParser(description="Auto-tune outcome-aware routing")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = load_tune_state()
    r, n, _ = compute_calibration()
    outcome_r, outcome_n = compute_outcome_aware_r()
    hidden = count_hidden_bridges()
    routing_collapsed, total_edges, zero_flow_edges = check_routing_collapse()
    
    now = time.time()
    time_since_last = now - state.get("last_tune_time", 0)
    freeze_until = state.get("freeze_until", 0)
    is_frozen = now < freeze_until

    print(f"{'='*60}")
    print(f"AUTO-TUNE STATUS")
    print(f"{'='*60}")
    print(f"  Flow r:              {r:.4f} (baseline: ~0.03, target: {R_TARGET})")
    print(f"  Outcome-aware r:     {outcome_r:.4f} (meaningful metric)")
    print(f"  Flow samples:        {n}")
    print(f"  Outcome samples:     {outcome_n}")
    print(f"  Hidden bridges:      {hidden}")
    collapse_str = f"YES ({zero_flow_edges}/{total_edges} zero flow)" if routing_collapsed else f"no ({zero_flow_edges}/{total_edges} zero flow)"
    print(f"  Routing collapse:    {collapse_str}")
    print(f"  Outcome bonus:       {state['outcome_bonus_weight']}")
    print(f"  Injection rate:      {state['injection_rate']}")
    print(f"  Novelty weight:      {state['novelty_weight']:.2f}")
    print(f"  Trust weight:        {state.get('trust_weight', 0.15):.2f}")
    print(f"  Last tune:           {time_since_last/60:.0f} min ago")
    print(f"  Total adjustments:   {len(state.get('adjustments', []))}")
    if is_frozen:
        remaining = (freeze_until - now) / 60
        print(f"  FROZEN:              outcome_bonus locked for {remaining:.0f} more min")

    if args.status:
        return

    # Don't tune if not enough data (but allow emergency override for critical hidden bridges)
    # Use outcome_n (meaningful samples) not flow_n (unreliable with few samples)
    EMERGENCY_HIDDEN_BRIDGE_THRESHOLD = 3
    STALE_TUNE_THRESHOLD = 1440  # 24 hours — force tune if last tune was this long ago
    emergency_mode = False
    stale_override = False
    if outcome_n < MIN_RESULTS_FOR_TUNING and hidden >= EMERGENCY_HIDDEN_BRIDGE_THRESHOLD:
        last_tune = state.get('last_tune_time', 0)
        stale_minutes = (now - last_tune) / 60 if last_tune > 0 else 9999
        if stale_minutes > STALE_TUNE_THRESHOLD:
            print(f"\n  STALE TUNE OVERRIDE: {outcome_n}/{MIN_RESULTS_FOR_TUNING} outcome samples but last tune was {stale_minutes:.0f} min ago (>{STALE_TUNE_THRESHOLD} min)")
            print(f"  Forcing tuning cycle despite low sample count — parameters stale.")
            stale_override = True
        else:
            print(f"\n  NEED MORE DATA: {outcome_n}/{MIN_RESULTS_FOR_TUNING} outcome samples")
            print(f"  Waiting for more results before tuning.")
            return
    elif outcome_n < MIN_RESULTS_FOR_TUNING and hidden < EMERGENCY_HIDDEN_BRIDGE_THRESHOLD:
        print(f"\n  EMERGENCY OVERRIDE: {outcome_n}/{MIN_RESULTS_FOR_TUNING} outcome samples but hidden bridges critically low ({hidden} < {EMERGENCY_HIDDEN_BRIDGE_THRESHOLD})")
        print(f"  Forcing tuning cycle despite low sample count.")
        emergency_mode = True
    elif hidden == 0:
        # hidden=0 with flow_norm detection means all high-SR edges have traffic.
        # This is the ideal state — no underrouted proven routes remain.
        # Only trigger emergency if flow r is negative (routing struggling).
        novelty_at_max = state.get("novelty_weight", 0.25) >= NOVELTY_WEIGHT_MAX
        if r < 0:
            # Flow r negative with hidden=0: OVER-EXPLORATION is the problem,
            # not under-exploration. High novelty pushes routing toward unexplored
            # edges that don't succeed, dragging flow r negative. Reduce novelty
            # to let proven routes dominate.
            print(f"\n  OVER-EXPLORATION OVERRIDE: hidden = 0, flow r = {r:.4f} < 0")
            print(f"  Reducing novelty to let proven routes dominate routing.")
            emergency_mode = True
        else:
            print(f"\n  HIDDEN BRIDGES = 0: All high-SR edges have traffic")
            print(f"  No emergency needed — routing is well-discovered.")
            # Still allow normal cooldown-respecting tuning
            pass

    # Crisis override: when hidden bridges are 5x+ target, bypass cooldown.
    # At crisis levels, the system is stuck in a saturation loop and needs
    # immediate consolidation pressure — waiting 5 more minutes worsens the problem.
    crisis_mode = hidden >= HIDDEN_BRIDGE_CRISIS
    manual_override_active = check_manual_override_cooldown(state, now)
    if crisis_mode:
        if manual_override_active:
            print(f"\n  CRISIS OVERRIDE BLOCKED: manual override cooldown active")
            print(f"  Respecting manual intervention — waiting for cooldown to expire")
            crisis_mode = False  # Don't bypass cooldown when manual override is active
        else:
            print(f"\n  CRISIS OVERRIDE: hidden bridges {hidden} >= {HIDDEN_BRIDGE_CRISIS} (5x target={HIDDEN_BRIDGE_TARGET})")
            print(f"  Bypassing cooldown for crisis consolidation.")

    # Enforce minimum time between adjustments (skip cooldown for emergency/crisis mode)
    if time_since_last < MIN_TIME_BETWEEN_ADJUSTMENTS and not args.dry_run and not emergency_mode and not crisis_mode:
        wait = MIN_TIME_BETWEEN_ADJUSTMENTS - time_since_last
        print(f"\n  COOLDOWN: Wait {wait/60:.1f} more min before next adjustment")
        return
    elif time_since_last < MIN_TIME_BETWEEN_ADJUSTMENTS and (emergency_mode or crisis_mode):
        if emergency_mode:
            print(f"\n  EMERGENCY: Bypassing cooldown for critical hidden bridges")
        elif crisis_mode:
            print(f"\n  CRISIS: Bypassing cooldown — hidden bridges at crisis level ({hidden} >= {HIDDEN_BRIDGE_CRISIS})")

    adjustments = []

    # Oscillation guard: check last 5 adjustments for ceiling trap patterns
    ceiling_trap_detected = detect_ceiling_trap_pattern(state.get("adjustments", []))

    # Apply freeze if ceiling trap oscillation detected
    if ceiling_trap_detected and not is_frozen and not args.dry_run:
        state["freeze_until"] = now + CEILING_TRAP_COOLDOWN
        adjustments.append(f"FREEZE: 3+ ceiling traps in last 5 — locking outcome_bonus for 30 min")

    ctx = SimpleNamespace(
        state=state,
        adjustments=adjustments,
        r=r,
        outcome_r=outcome_r,
        hidden=hidden,
        routing_collapsed=routing_collapsed,
        total_edges=total_edges,
        zero_flow_edges=zero_flow_edges,
        now=now,
        is_frozen=is_frozen,
        ceiling_trap_detected=ceiling_trap_detected,
    )

    # PRIMARY SIGNAL: outcome_r (correlates routing scores with success rates)
    # SECONDARY SIGNAL: flow r (correlates traffic volume with success — structurally near zero)
    # Outcome_r is the meaningful metric. Flow r is kept for backward compatibility
    # and specific traffic-analysis edge cases.

    _tune_outcome_bonus(ctx, args)

    _tune_novelty(ctx, args)

    _tune_injection_rate(ctx, args)

    _tune_trust_weight(ctx, args)

    _self_change_guard(ctx)

    # Report
    if adjustments:
        print(f"\n  ADJUSTMENTS ({'DRY RUN' if args.dry_run else 'APPLIED'}):")
        for adj in adjustments:
            print(f"    → {adj}")
        if not args.dry_run:
            state["adjustments"].extend([{"time": now, "r": r, "outcome_r": outcome_r, "hidden": hidden, "adj": a} for a in adjustments])
            state["last_r"] = r
            state["last_outcome_r"] = outcome_r
            state["last_hidden_bridges"] = hidden
            state["last_tune_time"] = now
            # Persist current hidden count to history for future trend detection
            new_history = state.get("hidden_history", []) + [hidden]
            if len(new_history) > 5:
                new_history = new_history[-5:]
            state["hidden_history"] = new_history
            save_tune_state(state)
    else:
        print(f"\n  NO ADJUSTMENTS NEEDED")
        # Still persist hidden_history even when no adjustments are made
        if not args.dry_run:
            new_history = state.get("hidden_history", []) + [hidden]
            if len(new_history) > 5:
                new_history = new_history[-5:]
            state["hidden_history"] = new_history
            state["last_hidden_bridges"] = hidden
            save_tune_state(state)

    print(f"\n  Ranges:")
    print(f"    outcome_bonus: [{OUTCOME_BONUS_MIN}-{OUTCOME_BONUS_MAX}] (current: {state['outcome_bonus_weight']})")
    print(f"    injection:    [{INJECTION_RATE_MIN}-{INJECTION_RATE_MAX}] (current: {state['injection_rate']})")
    print(f"    novelty:      [{NOVELTY_WEIGHT_MIN}-{NOVELTY_WEIGHT_MAX}] (current: {state['novelty_weight']:.2f})")
    print(f"    trust_weight: [{TRUST_WEIGHT_MIN}-{TRUST_WEIGHT_MAX}] (current: {state.get('trust_weight', 0.15):.2f})")


if __name__ == "__main__":
    main()
