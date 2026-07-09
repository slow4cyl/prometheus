#!/usr/bin/env python3
"""Domain health cache — computes persistence/influence scores and writes
a JSON cache for the curiosity scorer to read.

This closes the loop: governance metrics → scoring adjustments → better
transfer targeting.

Output: ~/.hermes/domain_health_cache.json

Format:
{
  "generated_at": 1781205447.985,
  "domains": {
    "woodcarving": {"persistence": 0.78, "influence": 0.22, "role": "producer"},
    "saddlery": {"persistence": 0.11, "influence": 0.11, "role": "consumer"},
    ...
  }
}
"""

import os, json, time, sqlite3, sys
from prometheus_paths import HERMES_HOME as _PP_HERMES_HOME
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERMES = _PP_HERMES_HOME
PROMETHEUS_DB = os.path.join(HERMES, "prometheus.db")
CACHE_PATH = os.path.join(HERMES, "domain_health_cache.json")


def compute_domain_health():
    """Compute persistence and influence scores for all domains."""
    conn = sqlite3.connect(PROMETHEUS_DB)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    c = conn.cursor()
    now = time.time()

    # Get all domains with enough data to measure
    c.execute("""SELECT domain, COUNT(*) as total,
                 MIN(CAST(created_at AS REAL)) as first_exp,
                 MAX(CAST(created_at AS REAL)) as last_exp
                 FROM experiments WHERE typeof(created_at) = 'real'
                 GROUP BY domain HAVING total >= 3""")
    domains = c.fetchall()

    result = {}
    for domain, total, first_exp, last_exp in domains:
        if not first_exp or not last_exp:
            continue

        age_hours = (now - first_exp) / 3600
        recency_hours = (now - last_exp) / 3600

        # PERSISTENCE: combines age, recency, and volume
        # High = old domain still receiving experiments
        age_score = min(age_hours / 168, 1.0)  # caps at 7 days
        recency_score = max(0, 1.0 - recency_hours / 48)  # decays over 2 days
        volume_score = min(total / 20, 1.0)  # caps at 20 experiments
        persistence = (age_score * 0.3 + recency_score * 0.4 + volume_score * 0.3)

        # INFLUENCE: outbound transfer rate
        # High = this domain's ideas are used by other domains
        c.execute("""SELECT COUNT(DISTINCT domain) FROM experiments
                     WHERE domain != ? AND typeof(created_at) = 'real'
                     AND UPPER(hypothesis) LIKE ?""",
                  (domain, f'[TRANSFER FROM {domain.upper()}%'))
        outbound_targets = c.fetchone()[0]

        import re
        c.execute("""SELECT hypothesis FROM experiments
                     WHERE domain=? AND typeof(created_at) = 'real'""", (domain,))
        inbound = 0
        for (h,) in c.fetchall():
            if h and '[TRANSFER' in h.upper():
                # Check this domain is NOT the source (i.e., it's the target)
                upper = h.upper()
                if 'FROM' in upper:
                    source_part = upper.split('FROM')[1][:30].lower()
                    if domain.lower() not in source_part:
                        inbound += 1

        total_transfers = inbound + outbound_targets
        influence = outbound_targets / max(total_transfers, 1) if total_transfers > 0 else 0

        # Determine role
        if persistence > 0.6 and influence > 0.3:
            role = "hub"
        elif persistence > 0.5:
            role = "producer"
        elif influence > 0.4:
            role = "reservoir"
        elif inbound > outbound_targets * 2:
            role = "consumer"
        elif persistence < 0.2 and recency_hours > 24:
            role = "transient"
        else:
            role = "mixed"

        result[domain] = {
            "persistence": round(persistence, 3),
            "influence": round(influence, 3),
            "role": role,
            "total": total,
            "outbound": outbound_targets,
        }

    conn.close()
    return result


def write_cache():
    """Compute and write the domain health cache."""
    domains = compute_domain_health()
    cache = {
        "generated_at": time.time(),
        "domains": domains,
    }
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)
    return cache


if __name__ == "__main__":
    cache = write_cache()
    # Summary
    roles = {}
    for d, info in cache["domains"].items():
        r = info["role"]
        roles[r] = roles.get(r, 0) + 1
    print(f"Domain health cache written: {len(cache['domains'])} domains")
    for role, cnt in sorted(roles.items(), key=lambda x: -x[1]):
        print(f"  {role}: {cnt}")
