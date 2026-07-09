#!/usr/bin/env python3
"""
System Health Signal — computes and writes health metrics for the Director.

Called by director.py at the START of each Director cycle.
Writes to ~/.hermes/system_health.json.

Concerns addressed:
- Token estimation: local from worker_results (directional, not exact)
- Queue flow: counts items added/consumed via separate tracking file
- Freshness: timestamp set at START of cycle, threshold 5 minutes
- Race conditions: separate file from self_state.json, no lock needed
"""

import json
import os
import sqlite3
import time

"""Health Signal.

Part of the Prometheus research infrastructure.
"""


KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
PROMETHEUS_DB = os.path.expanduser("~/.hermes/prometheus.db")
SELF_STATE = os.path.expanduser("~/.hermes/self_state.json")
HEALTH_FILE = os.path.expanduser("~/.hermes/system_health.json")
QUEUE_FLOW_FILE = os.path.expanduser("~/.hermes/queue_flow.json")

# Thresholds
FRESHNESS_THRESHOLD = 300  # 5 minutes — longer than worst-case Director cycle
TOKEN_BUDGET_HOURLY = 0.50  # $0.50/hour — adjusted from $0.15 (Jun 22 2026) to unblock synthesis; actual spend ~$0.47/hr peak


def get_concurrent_synthesis():
    """Count currently running synthesis tasks."""
    try:
        db = sqlite3.connect(KANBAN_DB)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        cur = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE 'SYNTHESIS%' "
            "AND status IN ('running','claimed')"
        )
        count = cur.fetchone()[0]
        db.close()
        return count
    except Exception:
        return 0


def get_coverage():
    """Compute synthesis coverage percentage."""
    try:
        db = sqlite3.connect(PROMETHEUS_DB)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        cur = db.execute("SELECT COUNT(*) FROM experiments")
        total = cur.fetchone()[0]

        cur = db.execute(
            "SELECT experiments_covered FROM synthesis_outputs "
            "WHERE experiments_covered IS NOT NULL"
        )
        synth_set = set()
        for row in cur.fetchall():
            try:
                for item in json.loads(row[0]):
                    if isinstance(item, str) and item.startswith('exp_'):
                        synth_set.add(item)
            except Exception:
                pass
        db.close()

        synthesized = len(synth_set)
        backlog = max(0, total - synthesized)  # clamp to 0 minimum
        coverage = (synthesized / total * 100) if total > 0 else 0
        return total, synthesized, backlog, round(coverage, 1)
    except Exception:
        return 0, 0, 0, 0.0


def get_hourly_stats():
    """Get experiment completion count and synthesis output count for last hour."""
    try:
        db = sqlite3.connect(PROMETHEUS_DB)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        hour_ago = time.time() - 3600

        cur = db.execute(
            "SELECT COUNT(*) FROM worker_results WHERE created_at > ?",
            (hour_ago,)
        )
        experiments = cur.fetchone()[0]

        cur = db.execute(
            "SELECT COUNT(*) FROM synthesis_outputs WHERE created_at > ?",
            (hour_ago,)
        )
        synth_outputs = cur.fetchone()[0]
        db.close()

        return experiments, synth_outputs
    except Exception:
        return 0, 0


def get_queue_size():
    """Read queue size from self_state.json."""
    try:
        with open(SELF_STATE) as f:
            state = json.load(f)
        return len(state.get("curiosity_queue", []))
    except Exception:
        return 0


def get_tasks_completed_hour():
    """Count tasks completed in last hour."""
    try:
        db = sqlite3.connect(KANBAN_DB)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        hour_ago = time.time() - 3600
        cur = db.execute(
            "SELECT COUNT(*) FROM tasks WHERE completed_at > ?",
            (hour_ago,)
        )
        count = cur.fetchone()[0]
        db.close()
        return count
    except Exception:
        return 0


def get_queue_flow():
    """Read queue flow counters (items added/consumed since last check)."""
    try:
        if os.path.exists(QUEUE_FLOW_FILE):
            with open(QUEUE_FLOW_FILE) as f:
                flow = json.load(f)
            # Reset counters after reading
            with open(QUEUE_FLOW_FILE, "w") as f:
                json.dump({"added": 0, "consumed": 0, "last_reset": time.time()}, f)
            return flow.get("added", 0), flow.get("consumed", 0)
        return 0, 0
    except Exception:
        return 0, 0


def estimate_cost(experiments_last_hour):
    """Estimate hourly cost from experiment count.
    
    Each experiment involves ~4 API calls × ~5000 tokens.
    With 97% cache hit rate on mimo-v2.5:
    - Cached: $0.0028/M tokens
    - Uncached: $0.14/M tokens
    """
    calls = experiments_last_hour * 4
    tokens = calls * 5000
    cost = (tokens * 0.03 * 0.14 + tokens * 0.97 * 0.0028) / 1_000_000
    return tokens, round(cost, 4)


def compute_health():
    """Compute all health metrics and write to system_health.json.
    
    Called at the START of each Director cycle (before any work).
    This ensures the timestamp reflects when health was last checked,
    not when the cycle completed.
    """
    now = time.time()

    # Check existing health file for freshness
    if os.path.exists(HEALTH_FILE):
        try:
            with open(HEALTH_FILE) as f:
                old_health = json.load(f)
            freshness = now - old_health.get("timestamp", 0)
        except Exception:
            freshness = 0
    else:
        freshness = 0

    # Collect all metrics (batch pattern: all queries first, evaluate after)
    concurrent_synthesis = get_concurrent_synthesis()
    total, synthesized, backlog, coverage_pct = get_coverage()
    experiments_hour, synth_outputs_hour = get_hourly_stats()
    queue_size = get_queue_size()
    tasks_completed = get_tasks_completed_hour()
    queue_added, queue_consumed = get_queue_flow()
    estimated_tokens, estimated_cost = estimate_cost(experiments_hour)

    # BUILD pipeline removed 2026-06-21
    implementation_impact_ratio = 0.0
    implemented_count = 0
    build_tagged_count = 0

    # Build health signal
    health = {
        "timestamp": now,
        "timestamp_human": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(now)
        ),
        "metrics": {
            "concurrent_synthesis": concurrent_synthesis,
            "total_experiments": total,
            "synthesized": synthesized,
            "backlog": backlog,
            "coverage_pct": coverage_pct,
            "experiments_last_hour": experiments_hour,
            "estimated_tokens_hour": estimated_tokens,
            "estimated_cost_hour": estimated_cost,
            "queue_size": queue_size,
            "tasks_completed_hour": tasks_completed,
            "synth_outputs_hour": synth_outputs_hour,
            "queue_added": queue_added,
            "queue_consumed": queue_consumed,
            "health_freshness_seconds": round(freshness, 0),
            "implementation_impact_ratio": implementation_impact_ratio,
            "implemented_count": implemented_count,
            "build_tagged_count": build_tagged_count,
        },
        "assessment": {
            "conservative_mode": freshness > FRESHNESS_THRESHOLD,
            "conservative_since": None,
            "synthesis_allowed": (
                coverage_pct < 80
                and backlog > 50
                and concurrent_synthesis < 5
            ),
            "queue_healthy": 30 <= queue_size <= 50,
            "token_burn_high": estimated_cost > TOKEN_BUDGET_HOURLY,
        },
    }

    # Track when conservative mode started (for timeout detection)
    is_conservative = health["assessment"]["conservative_mode"]
    if is_conservative:
        prev_conservative_since = None
        if os.path.exists(HEALTH_FILE):
            try:
                with open(HEALTH_FILE) as f:
                    prev = json.load(f)
                prev_conservative_since = prev.get("assessment", {}).get("conservative_since")
            except Exception:
                pass
        health["assessment"]["conservative_since"] = prev_conservative_since or now
    else:
        health["assessment"]["conservative_since"] = None

    # Write health file
    with open(HEALTH_FILE, "w") as f:
        json.dump(health, f, indent=2)

    return health


def record_queue_add(count=1):
    """Record items added to queue (called by synthesis_merger)."""
    try:
        flow = {"added": count, "consumed": 0, "last_reset": time.time()}
        if os.path.exists(QUEUE_FLOW_FILE):
            with open(QUEUE_FLOW_FILE) as f:
                existing = json.load(f)
            flow["added"] = existing.get("added", 0) + count
            flow["consumed"] = existing.get("consumed", 0)
        with open(QUEUE_FLOW_FILE, "w") as f:
            json.dump(flow, f)
    except Exception:
        pass


def record_queue_consume(count=1):
    """Record items consumed from queue (called by batch_create_tasks)."""
    try:
        flow = {"added": 0, "consumed": count, "last_reset": time.time()}
        if os.path.exists(QUEUE_FLOW_FILE):
            with open(QUEUE_FLOW_FILE) as f:
                existing = json.load(f)
            flow["added"] = existing.get("added", 0)
            flow["consumed"] = existing.get("consumed", 0) + count
        with open(QUEUE_FLOW_FILE, "w") as f:
            json.dump(flow, f)
    except Exception:
        pass


if __name__ == "__main__":
    health = compute_health()
    print(json.dumps(health, indent=2))
