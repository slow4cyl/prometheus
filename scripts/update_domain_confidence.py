#!/usr/bin/env python3
"""
Update domain confidence scores based on actual experiment data.

Domain confidence is derived from:
  1. Experiment count (log-scaled, more experiments = higher confidence)
  2. Average confidence_change across experiments in the domain
  3. Tag distribution (CONFIRMED/REFUTED/SUPPORTED weighted)

Runs in-place on prometheus.db. Safe to run repeatedly.

Usage:
    python3 update_domain_confidence.py          # Update all domains
    python3 update_domain_confidence.py --dry-run # Show what would change
    python3 update_domain_confidence.py --domain machine_learning  # Update one domain
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import time

# Canonical main hermes dir — derived from script location to avoid HOME override issues

"""CLI tool: Update Domain Confidence.

Usage: python3 update_domain_confidence.py [options]
"""

_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(os.path.join(_hermes_home_env, "prometheus_db.py")) else _HERMES_MAIN

sys.path.insert(0, HERMES_HOME)
from db_retry import get_db

DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")


def compute_domain_confidence(conn, domain_name):
    """Compute confidence score for a domain based on experiment data."""
    # Get all completed experiments for this domain
    rows = conn.execute(
        "SELECT result, confidence_change, tags FROM experiments WHERE domain = ? AND status = 'completed'",
        (domain_name,)
    ).fetchall()

    if not rows:
        return 0.5  # Default for domains with no experiments

    n_experiments = len(rows)

    # Component 1: Experiment count (log-scaled, 0-35 points)
    # Uses sigmoid-like scaling: fast initial rise, diminishing returns
    # 1 exp = ~12, 5 exp = ~22, 10 exp = ~27, 20 exp = ~31, 50+ = ~34-35
    if n_experiments == 0:
        count_score = 0
    else:
        count_score = min(35, 12 + 23 * (1 - math.exp(-n_experiments / 15)))

    # Component 2: Confidence change average (0-35 points)
    def _safe_float(v, default=0.0):
        try:
            return float(v)
        except (ValueError, TypeError):
            return default
    avg_conf = sum(_safe_float(r['confidence_change']) for r in rows) / n_experiments
    # Map avg_conf from [-1, 1] to [0, 35]
    # avg_conf=0 -> 17.5 (neutral), avg_conf=0.5 -> 26, avg_conf=-0.5 -> 9
    conf_score = max(0, min(35, 17.5 + avg_conf * 17.5))

    # Component 3: Tag quality (0-30 points)
    # Use startswith for verdict prefix (not `in` — avoids double-counting
    # when result text contains multiple verdict words like "REFUTED: ... SUPPORTED ...")
    confirmed = refuted = supported = partial = 0
    for r in rows:
        try:
            tags = json.loads(r['tags']) if r['tags'] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        result_upper = (r['result'] or '').upper().lstrip()
        # Check PARTIALLY first (more specific), then full verdicts
        if result_upper.startswith('PARTIALLY CONFIRMED') or result_upper.startswith('PARTIALLY SUPPORTED'):
            partial += 1
        elif result_upper.startswith('REFUTED') or result_upper.startswith('HYPOTHESIS REFUTED'):
            refuted += 1
        elif result_upper.startswith('CONFIRMED') or result_upper.startswith('SUPPORTED') or result_upper.startswith('MECHANISM CONFIRMED') or result_upper.startswith('HYPOTHESIS CONFIRMED'):
            confirmed += 1
        # Tags are additive (not mutually exclusive) — a single experiment can have multiple tags
        for t in tags:
            t_up = t.upper()
            if t_up == 'CONFIRMED':
                confirmed += 1
            elif t_up == 'REFUTED':
                refuted += 1
            elif t_up in ('SUPPORTED', 'BREAKTHROUGH', 'DISCOVERY'):
                supported += 1

    total_tagged = confirmed + refuted + supported + partial
    if total_tagged > 0:
        # Partial counts as 0.5 positive (between confirmed and neutral)
        positive_ratio = (confirmed + supported + partial * 0.5) / total_tagged
        tag_score = positive_ratio * 30
    else:
        tag_score = 15  # Neutral — no tags yet

    # Weighted sum: count(35) + conf(35) + tags(30) = 100 max
    raw = (count_score + conf_score + tag_score) / 100
    # Floor at 0.10 (not zero — domain exists, just unvalidated)
    confidence = max(0.10, min(0.99, raw))

    return round(confidence, 2)


def update_all_domains(dry_run=False, target_domain=None):
    """Update confidence for all (or one) domains."""
    with get_db() as conn:
        if target_domain:
            domains = conn.execute(
                "SELECT name, confidence FROM domains WHERE name = ?",
                (target_domain,)
            ).fetchall()
        else:
            domains = conn.execute(
                "SELECT name, confidence FROM domains ORDER BY name"
            ).fetchall()

        updated = 0
        for d in domains:
            new_conf = compute_domain_confidence(conn, d['name'])
            old_conf = d['confidence']

            if abs(new_conf - old_conf) < 0.01:
                continue  # No meaningful change

            if dry_run:
                print(f"  {d['name']}: {old_conf:.2f} -> {new_conf:.2f}")
            else:
                now = time.time()
                conn.execute(
                    "UPDATE domains SET confidence = ?, updated_at = ? WHERE name = ?",
                    (new_conf, now, d['name'])
                )
                print(f"  {d['name']}: {old_conf:.2f} -> {new_conf:.2f}")
            updated += 1

        if not dry_run:
            conn.commit()
            print(f"\nUpdated {updated} domains in {DB_PATH}")
        else:
            print(f"\nWould update {updated} domains (dry run)")

        return updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update domain confidence from experiment data")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--domain", type=str, help="Update only this domain")
    args = parser.parse_args()

    update_all_domains(dry_run=args.dry_run, target_domain=args.domain)
