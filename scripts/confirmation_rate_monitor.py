#!/usr/bin/env python3
"""
confirmation_rate_monitor.py — Track and alert on confirmation bias.

Based on exp_72526597: agent exhibits 75.2% confirmation rate (vs 50% null).
Mechanism: agent selects methods it knows work, designs experiments with
built-in success conditions. Same-domain topics confirm at 86-100%.

Target: 55-65% confirmation rate. Above 70% = experiment design too conservative.

Usage:
    python3 confirmation_rate_monitor.py              # Report current rates
    python3 confirmation_rate_monitor.py --recent 50  # Last N experiments only
    python3 confirmation_rate_monitor.py --domain <d>  # Rate for specific domain
"""

import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import os
import sqlite3
import sys
from collections import Counter

"""CLI tool: Confirmation Rate Monitor.

Usage: python3 confirmation_rate_monitor.py [options]
"""


DB_PATH = _PP_PROMETHEUS_DB
TARGET_RATE = 0.60  # 55-65% target, 60% center
ALERT_THRESHOLD = 0.70  # Above this = too conservative


def get_confirmation_rate(db, limit=None, domain=None):
    """Calculate confirmation rate from experiments."""
    query = """
        SELECT 
            CASE 
                WHEN result LIKE '%CONFIRMED%' OR result LIKE '%SUPPORTED%' THEN 'confirmed'
                WHEN result LIKE '%REFUTED%' OR result LIKE '%REJECTED%' THEN 'refuted'
                ELSE 'other'
            END as verdict,
            domain,
            id
        FROM experiments 
        WHERE status='completed' AND result IS NOT NULL AND length(result) > 20
    """
    params = []
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    query += " ORDER BY completed_at DESC"
    if limit:
        query += f" LIMIT {limit}"

    rows = db.execute(query, params).fetchall()
    return rows


def main():
    parser = argparse.ArgumentParser(description="Confirmation Rate Monitor")
    parser.add_argument("--recent", type=int, help="Only count last N experiments")
    parser.add_argument("--domain", type=str, help="Filter by domain")
    args = parser.parse_args()

    db = sqlite3.connect(DB_PATH, timeout=5)
    db.execute("PRAGMA busy_timeout=3000")

    rows = get_confirmation_rate(db, limit=args.recent, domain=args.domain)
    if not rows:
        print("No experiments found")
        db.close()
        return

    total = len(rows)
    confirmed = sum(1 for r in rows if r[0] == "confirmed")
    refuted = sum(1 for r in rows if r[0] == "refuted")
    other = sum(1 for r in rows if r[0] == "other")

    rate = confirmed / total if total > 0 else 0

    label = f"Last {args.recent}" if args.recent else "All"
    if args.domain:
        label += f" ({args.domain})"

    print(f"Confirmation Rate ({label}): {rate:.1%} ({confirmed}/{total})")
    print(f"  Confirmed: {confirmed} ({confirmed/total*100:.1f}%)")
    print(f"  Refuted:   {refuted} ({refuted/total*100:.1f}%)")
    print(f"  Other:     {other} ({other/total*100:.1f}%)")

    if rate > ALERT_THRESHOLD:
        print(f"\n  ⚠ HIGH CONFIRMATION RATE: {rate:.1%} > {ALERT_THRESHOLD:.0%}")
        print(f"  Experiment design may be too conservative. Target: {TARGET_RATE:.0%}")
    elif rate > TARGET_RATE + 0.05:
        print(f"\n  ⚠ ABOVE TARGET: {rate:.1%} > {TARGET_RATE+0.05:.0%}")
    elif rate < TARGET_RATE - 0.10:
        print(f"\n  ⚠ LOW CONFIRMATION RATE: {rate:.1%} — experiments may be too ambitious")
    else:
        print(f"\n  ✓ Within target range ({TARGET_RATE-0.05:.0%}-{TARGET_RATE+0.05:.0%})")

    # Domain breakdown
    if not args.domain and not args.recent:
        print(f"\nDomain breakdown (top 15 by confirmation rate):")
        domain_stats = Counter()
        domain_confirmed = Counter()
        for verdict, domain, _ in rows:
            domain_stats[domain] += 1
            if verdict == "confirmed":
                domain_confirmed[domain] += 1

        ranked = []
        for domain, count in domain_stats.items():
            if count >= 5:
                conf = domain_confirmed.get(domain, 0)
                ranked.append((domain, conf, count, conf/count))

        for domain, conf, count, rate in sorted(ranked, key=lambda x: -x[3])[:15]:
            marker = " ⚠" if rate > 0.70 else ""
            pct = rate * 100
            print(f"  {domain:30s}: {conf:3d}/{count:3d} ({pct:.0f}%){marker}")

    db.close()


if __name__ == "__main__":
    main()
