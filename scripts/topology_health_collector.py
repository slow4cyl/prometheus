#!/usr/bin/env python3
"""
topology_health_collector.py — 5-minute topology snapshot pipeline.

Computes ecosystem-level and per-domain metrics from the current state,
stores them in topology_snapshots / topology_domain_snapshots, computes
derivatives from the previous snapshot, evaluates diagnosis rules, and
stores alerts in topology_alerts.

Run via cron every 5 minutes. Designed to be lightweight — uses cached
topology from topology_common.py and the existing embedding cache.
"""

import json
import math
import os
import sqlite3
import sys
from db_retry import get_db
import time
from collections import defaultdict

HERMES = os.path.expanduser("~/.hermes")
DB_PATH = os.path.join(HERMES, "prometheus.db")
EMBEDDING_CACHE = os.path.join(HERMES, "domain_embeddings.json")
EXPORT_PATH = os.path.join(HERMES, "topology_full_export.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def shannon_entropy(shares):
    """Shannon entropy of a probability distribution (in bits)."""
    h = 0.0
    for p in shares:
        if p > 0:
            h -= p * math.log2(p)
    return h


def gini(values):
    """Gini coefficient of a list of non-negative values."""
    if not values:
        return 0.0
    vals = sorted(values)
    n = len(vals)
    total = sum(vals)
    if total == 0:
        return 0.0
    cumulative = 0.0
    gini_sum = 0.0
    for i, v in enumerate(vals):
        cumulative += v
        gini_sum += (2 * (i + 1) - n - 1) * v
    return gini_sum / (n * total)


def pairwise_distances(centroids):
    """Compute pairwise cosine distances between centroid vectors."""
    domains = list(centroids.keys())
    if len(domains) < 2:
        return 0.0, float('inf'), 0.0

    distances = []
    for i in range(len(domains)):
        for j in range(i + 1, len(domains)):
            vi = centroids[domains[i]]
            vj = centroids[domains[j]]
            # Cosine similarity → distance
            dot = sum(a * b for a, b in zip(vi, vj))
            ni = math.sqrt(sum(a * a for a in vi))
            nj = math.sqrt(sum(b * b for b in vj))
            if ni > 0 and nj > 0:
                sim = dot / (ni * nj)
                dist = 1.0 - sim
                distances.append(dist)

    if not distances:
        return 0.0, float('inf'), 0.0

    mean_d = sum(distances) / len(distances)
    min_d = min(distances)
    variance = sum((d - mean_d) ** 2 for d in distances) / len(distances)
    return mean_d, min_d, variance


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_experiment_distribution(conn):
    """Get domain → experiment count from the experiments table."""
    rows = conn.execute(
        "SELECT domain, COUNT(*) as cnt FROM experiments "
        "WHERE domain IS NOT NULL GROUP BY domain ORDER BY cnt DESC"
    ).fetchall()
    total = sum(r[1] for r in rows)
    if total == 0:
        return {}, 0
    return {r[0]: r[1] for r in rows}, total


def load_topology_data():
    """Load topology from topology_common.py (uses cached routing logs)."""
    sys.path.insert(0, os.path.join(HERMES, "scripts"))
    from topology_common import load_topology
    edges, degree, betweenness = load_topology(use_cache=True)
    return edges, degree, betweenness


def load_centroids():
    """Load embedding centroids from cache."""
    if not os.path.exists(EMBEDDING_CACHE):
        return {}
    with open(EMBEDDING_CACHE) as f:
        return json.load(f)


def load_community_assignments():
    """Load community assignments from the latest topology export."""
    if not os.path.exists(EXPORT_PATH):
        return {}
    try:
        with open(EXPORT_PATH) as f:
            data = json.load(f)
        nodes = data.get("nodes", {})
        return {
            nid: n.get("community_id", "unknown")
            for nid, n in nodes.items()
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

def compute_ecosystem_signals(conn, edges, degree, betweenness, centroids):
    """Compute all ecosystem-level metrics for the current snapshot."""

    # Experiment distribution
    domain_exps, total_exps = load_experiment_distribution(conn)

    # Routing entropy
    if total_exps > 0:
        shares = [cnt / total_exps for cnt in domain_exps.values()]
        entropy = shannon_entropy(shares)
    else:
        entropy = 0.0

    # Concentration
    sorted_domains = sorted(domain_exps.items(), key=lambda x: -x[1])
    top1_domain = sorted_domains[0][0] if sorted_domains else None
    top1_share = sorted_domains[0][1] / total_exps if total_exps and sorted_domains else 0.0
    top3_share = sum(cnt for _, cnt in sorted_domains[:3]) / total_exps if total_exps else 0.0
    top10_share = sum(cnt for _, cnt in sorted_domains[:10]) / total_exps if total_exps else 0.0

    # Gini coefficients
    exp_counts = list(domain_exps.values())
    pagerank_gini = gini(exp_counts)

    routing_values = list(degree.values())
    routing_gini = gini(routing_values)

    # Centroid distances
    if centroids:
        mean_d, min_d, var_d = pairwise_distances(centroids)
    else:
        mean_d, min_d, var_d = 0.0, float('inf'), 0.0

    # Community count
    communities = load_community_assignments()
    community_count = len(set(communities.values())) if communities else 0

    # Edge novelty (edges that exist in routing but not in previous snapshot)
    # Computed later via derivative comparison
    edge_count = len(edges)
    node_count = len(degree)
    total_traffic = sum(degree.values())

    # Clustering coefficient (approximate from edge set)
    clustering = _approx_clustering(edges, degree)

    return {
        "node_count": node_count,
        "edge_count": edge_count,
        "total_routing_traffic": total_traffic,
        "routing_entropy": entropy,
        "top1_domain": top1_domain,
        "top1_share": top1_share,
        "top3_share": top3_share,
        "top10_share": top10_share,
        "pagerank_gini": pagerank_gini,
        "routing_gini": routing_gini,
        "mean_centroid_distance": mean_d,
        "min_centroid_distance": min_d,
        "centroid_variance": var_d,
        "community_count": community_count,
        "clustering_coefficient": clustering,
    }


def _approx_clustering(edges, degree):
    """Approximate global clustering coefficient from edge set."""
    if not edges or not degree:
        return 0.0
    # Triangles / connected triples (approximate)
    # For large graphs, sampling would be better; here we compute exactly
    # but only on the edge set (which is <10K edges)
    adj = defaultdict(set)
    for (s, t) in edges:
        adj[s].add(t)
        adj[t].add(s)

    triangles = 0
    triples = 0
    nodes = list(adj.keys())
    for node in nodes:
        neighbors = list(adj[node])
        k = len(neighbors)
        if k < 2:
            continue
        triples += k * (k - 1) / 2
        for i in range(k):
            for j in range(i + 1, k):
                if neighbors[j] in adj[neighbors[i]]:
                    triangles += 1

    if triples == 0:
        return 0.0
    return triangles / triples


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def compute_activity_context(conn, since_timestamp):
    """Count what happened since the last snapshot."""
    try:
        exp = conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE created_at > ?",
            (since_timestamp,)
        ).fetchone()[0]
        # Also count experiments that were completed (status changed) since last snapshot
        exp_completed = conn.execute(
            "SELECT COUNT(*) FROM experiments WHERE status = 'completed' AND completed_at > ?",
            (since_timestamp,)
        ).fetchone()[0]
        wr = conn.execute(
            "SELECT COUNT(*) FROM worker_results WHERE created_at > ?",
            (since_timestamp,)
        ).fetchone()[0]
        tt = conn.execute(
            "SELECT COUNT(*) FROM transfer_tracking WHERE created_at > ?",
            (since_timestamp,)
        ).fetchone()[0]
        cc = conn.execute(
            "SELECT COUNT(*) FROM code_changes WHERE created_at > ?",
            (since_timestamp,)
        ).fetchone()[0]
        so = conn.execute(
            "SELECT COUNT(*) FROM synthesis_outputs WHERE created_at > ?",
            (since_timestamp,)
        ).fetchone()[0]
        return {
            "experiments_completed": exp_completed,
            "experiments_created": exp,
            "worker_results_written": wr,
            "transfers_added": tt,
            "code_changes": cc,
            "synthesis_outputs": so,
        }
    except Exception:
        return {"experiments_completed": 0, "experiments_created": 0,
                "worker_results_written": 0,
                "transfers_added": 0, "code_changes": 0, "synthesis_outputs": 0}


def store_snapshot(conn, signals, timestamp, activity):
    """Store ecosystem-level snapshot. Returns snapshot_id."""
    cur = conn.execute(
        """INSERT INTO topology_snapshots
           (timestamp, node_count, edge_count, total_routing_traffic,
            routing_entropy, top1_domain, top1_share, top3_share, top10_share,
            pagerank_gini, routing_gini, mean_centroid_distance,
            min_centroid_distance, centroid_variance, community_count,
            edge_novelty_rate, clustering_coefficient)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (timestamp, signals["node_count"], signals["edge_count"],
         signals["total_routing_traffic"], signals["routing_entropy"],
         signals["top1_domain"], signals["top1_share"], signals["top3_share"],
         signals["top10_share"], signals["pagerank_gini"],
         signals["routing_gini"], signals["mean_centroid_distance"],
         signals["min_centroid_distance"], signals["centroid_variance"],
         signals["community_count"], 0.0,  # edge_novelty computed later
         signals["clustering_coefficient"])
    )
    return cur.lastrowid


def store_domain_snapshots(conn, snapshot_id, degree, betweenness, domain_exps, communities, centroids):
    """Store per-domain metrics."""
    all_domains = set(degree.keys()) | set(domain_exps.keys())
    rows = []
    for domain in all_domains:
        rows.append((
            snapshot_id,
            domain,
            0.0,  # pagerank - not computed here, use export if available
            betweenness.get(domain, 0.0),
            domain_exps.get(domain, 0),
            degree.get(domain, 0),
            communities.get(domain, "unknown"),
            _centroid_norm(centroids.get(domain)),
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO topology_domain_snapshots
           (snapshot_id, domain, pagerank, betweenness, experiment_count,
            routing_degree, community_id, centroid_norm)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows
    )


def _centroid_norm(centroid):
    """L2 norm of a centroid vector."""
    if not centroid:
        return 0.0
    return math.sqrt(sum(x * x for x in centroid))


# ---------------------------------------------------------------------------
# Derivatives
# ---------------------------------------------------------------------------

def compute_derivatives(conn, current, snapshot_id):
    """Compute velocity and acceleration from the previous snapshot."""
    prev = conn.execute(
        "SELECT routing_entropy, top3_share, mean_centroid_distance, "
        "community_count, clustering_coefficient "
        "FROM topology_snapshots ORDER BY snapshot_id DESC LIMIT 1 OFFSET 1"
    ).fetchone()

    if not prev:
        return None  # No previous snapshot to compare

    derivatives = {}
    metrics = ["routing_entropy", "top3_share", "mean_centroid_distance",
               "community_count", "clustering_coefficient"]

    for i, m in enumerate(metrics):
        curr_val = current.get(m, 0)
        prev_val = prev[i] if prev[i] is not None else 0
        velocity = curr_val - prev_val
        derivatives[f"{m}_velocity"] = velocity

    # Need two previous snapshots for acceleration
    prev2 = conn.execute(
        "SELECT routing_entropy, top3_share, mean_centroid_distance "
        "FROM topology_snapshots ORDER BY snapshot_id DESC LIMIT 1 OFFSET 2"
    ).fetchone()

    if prev2:
        for i, m in enumerate(["routing_entropy", "top3_share", "mean_centroid_distance"]):
            curr_vel = derivatives.get(f"{m}_velocity", 0)
            prev_vel = (prev[i] - prev2[i]) if prev2[i] is not None else 0
            derivatives[f"{m}_acceleration"] = curr_vel - prev_vel

    return derivatives


# ---------------------------------------------------------------------------
# Diagnosis rules
# ---------------------------------------------------------------------------

def evaluate_rules(current, derivatives):
    """Evaluate simple diagnosis rules. Returns list of alerts."""
    alerts = []

    if not derivatives:
        return alerts

    # Rule 1: Top-3 concentration acceleration
    top3_accel = derivatives.get("top3_share_acceleration")
    if top3_accel is not None and top3_accel > 0.02:  # >2% acceleration
        alerts.append({
            "alert_type": "concentration_event",
            "severity": "warning" if top3_accel > 0.05 else "info",
            "message": f"Top-3 concentration accelerating: +{top3_accel:.4f} per interval",
            "metrics": {"top3_share": current.get("top3_share"), "acceleration": top3_accel}
        })

    # Rule 2: Entropy decreasing + concentration increasing
    entropy_vel = derivatives.get("routing_entropy_velocity", 0)
    top3_vel = derivatives.get("top3_share_velocity", 0)
    if entropy_vel < -0.01 and top3_vel > 0.01:
        alerts.append({
            "alert_type": "emerging_attractor",
            "severity": "warning",
            "message": f"Entropy falling ({entropy_vel:+.4f}) while concentration rising ({top3_vel:+.4f})",
            "metrics": {"entropy_velocity": entropy_vel, "top3_velocity": top3_vel}
        })

    # Rule 3: Centroid convergence
    centroid_vel = derivatives.get("mean_centroid_distance_velocity", 0)
    if centroid_vel < -0.005:
        alerts.append({
            "alert_type": "semantic_convergence",
            "severity": "info",
            "message": f"Mean centroid distance decreasing: {centroid_vel:+.6f}",
            "metrics": {"centroid_velocity": centroid_vel}
        })

    # Rule 4: Community churn (community count changed)
    community_vel = derivatives.get("community_count_velocity", 0)
    if abs(community_vel) > 2:
        alerts.append({
            "alert_type": "structural_instability",
            "severity": "info",
            "message": f"Community count changed by {community_vel:+.0f}",
            "metrics": {"community_velocity": community_vel}
        })

    # Rule 5: Routing entropy critically low
    if current.get("routing_entropy", 1.0) < 2.0:
        alerts.append({
            "alert_type": "low_entropy",
            "severity": "warning",
            "message": f"Routing entropy critically low: {current['routing_entropy']:.4f}",
            "metrics": {"entropy": current["routing_entropy"]}
        })

    return alerts


def store_alerts(conn, alerts, snapshot_id, timestamp):
    """Store alerts in the database."""
    for alert in alerts:
        conn.execute(
            """INSERT INTO topology_alerts
               (snapshot_id, timestamp, alert_type, severity, message, metrics_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (snapshot_id, timestamp, alert["alert_type"], alert["severity"],
             alert["message"], json.dumps(alert.get("metrics", {})))
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect():
    """Run one collection cycle.

    Uses a longer busy_timeout than other crons because apply_worker_results
    holds a long-running open write txn (commits every 50 rows) and we
    collide with it under load. With the default 800ms timeout the snapshot
    INSERT loses the race and the whole cycle aborts. A 30s timeout lets us
    wait it out cleanly — this cron only runs every 10 minutes so a single
    long wait here is cheap.
    """
    now = time.time()
    import db_retry
    saved_timeout = db_retry.BUSY_TIMEOUT
    db_retry.BUSY_TIMEOUT = 30000
    try:
        conn = get_db(DB_PATH)
    finally:
        db_retry.BUSY_TIMEOUT = saved_timeout

    try:
        # Load data
        edges, degree, betweenness = load_topology_data()
        centroids = load_centroids()
        communities = load_community_assignments()
        domain_exps, total_exps = load_experiment_distribution(conn)

        # Compute ecosystem signals
        signals = compute_ecosystem_signals(conn, edges, degree, betweenness, centroids)

        # Compute activity context since last snapshot
        prev_ts = conn.execute(
            "SELECT timestamp FROM topology_snapshots ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        since_ts = prev_ts[0] if prev_ts else 0
        activity = compute_activity_context(conn, since_ts)

        # Store snapshot
        snapshot_id = store_snapshot(conn, signals, now, activity)

        # Store per-domain
        store_domain_snapshots(conn, snapshot_id, degree, betweenness,
                               domain_exps, communities, centroids)

        # Compute derivatives
        derivatives = compute_derivatives(conn, signals, snapshot_id)

        # Evaluate rules
        alerts = evaluate_rules(signals, derivatives)

        # Store alerts
        if alerts:
            store_alerts(conn, alerts, snapshot_id, now)

        conn.commit()

        # Print summary
        print(f"Snapshot #{snapshot_id} collected at {time.strftime('%H:%M:%S')}")
        print(f"  Nodes: {signals['node_count']}, Edges: {signals['edge_count']}")
        print(f"  Entropy: {signals['routing_entropy']:.4f}")
        print(f"  Top-3 share: {signals['top3_share']:.2%}")
        print(f"  Communities: {signals['community_count']}")
        print(f"  Centroid mean dist: {signals['mean_centroid_distance']:.6f}")
        if derivatives:
            print(f"  Entropy velocity: {derivatives.get('routing_entropy_velocity', 'N/A')}")
            print(f"  Top-3 acceleration: {derivatives.get('top3_share_acceleration', 'N/A')}")
        if alerts:
            print(f"  ALERTS: {len(alerts)}")
            for a in alerts:
                print(f"    [{a['severity'].upper()}] {a['alert_type']}: {a['message']}")
        else:
            print(f"  No alerts")

    finally:
        conn.close()


if __name__ == "__main__":
    collect()
