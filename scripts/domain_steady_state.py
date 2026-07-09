#!/usr/bin/env python3
"""Daily domain governance snapshot for steady-state tracking.
Compares today's metrics to yesterday's and reports significant changes.
"""

import sys, os, fcntl, json, time, sqlite3

HERMES = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
LOCK_PATH = os.path.join(HERMES, ".domain_steady_state.lock")
SNAPSHOT_DIR = os.path.join(HERMES, "domain_governance_snapshots")
PROMETHEUS_DB = os.path.join(HERMES, "prometheus.db")


def take_snapshot():
    """Capture current governance metrics."""
    conn = sqlite3.connect(PROMETHEUS_DB)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    c = conn.cursor()
    now = time.time()

    snapshot = {
        "timestamp": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_experiments": c.execute("SELECT COUNT(*) FROM experiments WHERE typeof(created_at)='real'").fetchone()[0],
        "total_domains": c.execute("SELECT COUNT(DISTINCT domain) FROM experiments").fetchone()[0],
        "canonical_domains": c.execute("SELECT COUNT(*) FROM (SELECT domain FROM experiments GROUP BY domain HAVING COUNT(*)>=10)").fetchone()[0],
        "small_domains": c.execute("SELECT COUNT(*) FROM (SELECT domain FROM experiments GROUP BY domain HAVING COUNT(*)<10)").fetchone()[0],
        "redirects": c.execute("SELECT COUNT(*) FROM domain_redirects").fetchone()[0],
    }

    # Gate health
    try:
        gh = c.execute("SELECT * FROM gate_health").fetchone()
        if gh:
            snapshot["gate_health"] = dict(zip(
                ['total_redirects','reversals','provisional','candidate','promoted','canonical_domains','small_domains'], gh))
    except Exception:
        pass

    # Transfer concentration index (GPT's Transfer Diversity Index)
    try:
        c.execute("""SELECT domain, COUNT(*) as cnt FROM experiments
                     WHERE hypothesis LIKE '%[TRANSFER%' AND typeof(created_at) = 'real'
                     GROUP BY domain ORDER BY cnt DESC""")
        transfer_targets = c.fetchall()
        total_inbound = sum(cnt for _, cnt in transfer_targets)
        if total_inbound > 0 and transfer_targets:
            top10_count = sum(cnt for _, cnt in transfer_targets[:10])
            top10_pct = top10_count / total_inbound * 100
            unique_targets = len(transfer_targets)
        else:
            top10_pct = 0
            unique_targets = 0
        snapshot["transfer_concentration"] = {
            "total_inbound": total_inbound,
            "unique_targets": unique_targets,
            "top10_pct": round(top10_pct, 1),
        }
    except Exception:
        pass

    conn.close()
    return snapshot


def compare_to_yesterday(today):
    """Compare today's snapshot to the most recent previous one."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    files = sorted([f for f in os.listdir(SNAPSHOT_DIR) if f.startswith("snapshot_") and f.endswith(".json")])

    if len(files) < 2:
        return None  # No previous snapshot to compare

    # Load second-to-last snapshot
    prev_path = os.path.join(SNAPSHOT_DIR, files[-2])
    with open(prev_path) as f:
        prev = json.load(f)

    changes = {}
    for key in ["total_domains", "canonical_domains", "small_domains", "redirects"]:
        old = prev.get(key, 0)
        new = today.get(key, 0)
        if old > 0:
            pct = abs(new - old) / old * 100
            if pct > 10:
                changes[key] = f"{old} -> {new} ({pct:.0f}% change)"
        elif new > 0:
            changes[key] = f"0 -> {new} (new)"

    # Check reversals
    prev_rev = prev.get("gate_health", {}).get("reversals", 0)
    today_rev = today.get("gate_health", {}).get("reversals", 0)
    if today_rev > prev_rev:
        changes["reversals"] = f"{prev_rev} -> {today_rev} (NEW REVERSAL)"

    # Check promotions
    prev_promo = prev.get("gate_health", {}).get("promoted", 0)
    today_promo = today.get("gate_health", {}).get("promoted", 0)
    if today_promo > prev_promo:
        changes["promotions"] = f"{prev_promo} -> {today_promo} (NEW PROMOTION)"

    # Check transfer concentration drift (>5pp change = alert)
    prev_conc = prev.get("transfer_concentration", {}).get("top10_pct", 0)
    today_conc = today.get("transfer_concentration", {}).get("top10_pct", 0)
    if abs(today_conc - prev_conc) > 5:
        direction = "MORE concentrated" if today_conc > prev_conc else "LESS concentrated"
        changes["transfer_concentration"] = f"{prev_conc:.0f}% -> {today_conc:.0f}% ({direction})"

    return changes if changes else None


if __name__ == "__main__":
    # Prevent overlapping runs
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    try:
        snapshot = take_snapshot()

        # Save snapshot
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        path = os.path.join(SNAPSHOT_DIR, f"snapshot_{int(snapshot['timestamp'])}.json")
        with open(path, "w") as f:
            json.dump(snapshot, f, indent=2)

        # Compare to yesterday
        changes = compare_to_yesterday(snapshot)
        if changes:
            print("DOMAIN GOVERNANCE — Significant changes detected:")
            for k, v in changes.items():
                print(f"  {k}: {v}")
        # If no changes, stay silent (watchdog pattern)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
