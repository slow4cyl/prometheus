#!/usr/bin/env python3
"""
normalize_all_domains.py — One-shot normalization of ALL domain strings across
experiments, worker_results, and domains tables.

Mechanical normalization: lowercase, hyphens/spaces -> underscores, strip junk.
Semantic merges: maps known synonyms to canonical forms.
"""

import sqlite3
import re
import sys
from collections import Counter
from db_retry import get_db

from prometheus_paths import PROMETHEUS_DB
DB_PATH = sys.argv[1] if len(sys.argv) > 1 else PROMETHEUS_DB

# --- Semantic merge map: normalized_key -> canonical domain ---
# These are cases where the "same" domain has conceptually different names
# that normalization alone won't fix.
# SEMANTIC_MERGES removed 2026-07-09: this was a stale fourth copy of the
# domain policy that still carried every lossy mapping banned in
# write_worker_result.normalize_domain on 2026-06-08 ('general'->'calibration',
# 'rag*'->'injection_detection', 'defense'/'attack', 'synthesis'/'meta', ...)
# — and this script bulk-APPLIES its map to experiments/worker_results every
# 15 minutes via domain-taxonomy-maintenance. Canonicalization now delegates
# to the single source of truth. Do not re-inline a dict here.


def normalize_key(s):
    """Mechanical normalization: lowercase, replace separators, strip."""
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r'[\s\-/]+', '_', s)
    s = re.sub(r'[^a-z0-9_]', '', s)
    s = re.sub(r'_+', '_', s)
    s = s.strip('_')
    return s


def canonical_for(domain):
    """Map a domain string to its canonical form (single source of truth)."""
    if not domain:
        return ""
    from write_worker_result import normalize_domain
    return normalize_domain(domain) or ""


def main():
    conn = get_db(DB_PATH)
    # db_retry sets busy_timeout=800ms; this is the largest transaction in the
    # taxonomy chain, so keep the 30s wait (already more patient than the retry loop).
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    c = conn.cursor()

    # --- Step 1: Collect all unique domains and count usage ---
    all_domains = Counter()

    c.execute("SELECT domain, COUNT(*) FROM experiments GROUP BY domain")
    for d, cnt in c.fetchall():
        all_domains[d] += cnt

    c.execute("SELECT domain, COUNT(*) FROM worker_results WHERE domain IS NOT NULL AND domain != '' GROUP BY domain")
    for d, cnt in c.fetchall():
        all_domains[d] += cnt

    print(f"Total unique domain strings: {len(all_domains)}")

    # --- Step 2: Build canonical mapping ---
    canonical_map = {}  # original -> canonical
    for domain in all_domains:
        canonical_map[domain] = canonical_for(domain)

    # Count how many canonical domains we'll have
    canonical_counts = Counter()
    for orig, canon in canonical_map.items():
        canonical_counts[canon] += all_domains[orig]

    print(f"Canonical domains after normalization: {len(canonical_counts)}")
    print(f"\nTop 30 canonical domains:")
    for d, cnt in canonical_counts.most_common(30):
        print(f"  {d}: {cnt}")

    # --- Step 3: Apply to experiments table ---
    c.execute("SELECT COUNT(*) FROM experiments")
    total_exp = c.fetchone()[0]
    updated_exp = 0
    c.execute("SELECT id, domain FROM experiments")
    exp_updates = []
    for eid, domain in c.fetchall():
        canon = canonical_map.get(domain, domain)
        if canon != domain:
            exp_updates.append((canon, eid))
    if exp_updates:
        c.executemany("UPDATE experiments SET domain = ? WHERE id = ?", exp_updates)
        updated_exp = len(exp_updates)
    print(f"\nExperiments updated: {updated_exp}/{total_exp}")

    # --- Step 4: Apply to worker_results table ---
    c.execute("SELECT COUNT(*) FROM worker_results")
    total_wr = c.fetchone()[0]
    updated_wr = 0
    c.execute("SELECT id, domain FROM worker_results WHERE domain IS NOT NULL AND domain != ''")
    wr_updates = []
    for wid, domain in c.fetchall():
        canon = canonical_map.get(domain, domain)
        if canon != domain:
            wr_updates.append((canon, wid))
    if wr_updates:
        c.executemany("UPDATE worker_results SET domain = ? WHERE id = ?", wr_updates)
        updated_wr = len(wr_updates)
    # Empty domains are LEFT ALONE for the embedding classifier (the 2026-06-08
    # policy): sweeping them into 'calibration' was the original mislabeling bug.
    c.execute("SELECT COUNT(*) FROM worker_results WHERE domain IS NULL OR domain = ''")
    empty_count = c.fetchone()[0]
    print(f"Worker results updated: {updated_wr}/{total_wr}")
    print(f"Worker results with empty domain (left for embedding classifier): {empty_count}")

    # --- Step 5: Rebuild domains table ---
    # Drop all entries, rebuild from experiment domains ONLY
    # (worker_results has one-off domain strings that shouldn't be canonical)
    c.execute("DELETE FROM domains")

    # Get domain counts from experiments only (post-normalization)
    # Skip NULL/empty domain strings — they cause NOT NULL constraint failures
    c.execute("SELECT domain, COUNT(*) as cnt FROM experiments WHERE domain IS NOT NULL AND domain != '' GROUP BY domain ORDER BY cnt DESC")
    exp_domains = c.fetchall()

    import time
    now = time.time()
    for domain, cnt in exp_domains:
        # Confidence based on experiment count (more experiments = higher confidence)
        confidence = min(0.5 + cnt / 2000.0, 0.95)
        c.execute("INSERT INTO domains (name, confidence, created_at, updated_at) VALUES (?, ?, ?, ?)",
                  (domain, confidence, now, now))

    c.execute("SELECT COUNT(*) FROM domains")
    new_domain_count = c.fetchone()[0]
    print(f"\nDomains table rebuilt: {new_domain_count} entries")

    # --- Step 6: Report remaining non-canonical ---
    c.execute("SELECT domain, COUNT(*) FROM experiments GROUP BY domain")
    remaining_non_canon = 0
    for d, cnt in c.fetchall():
        c2 = conn.cursor()
        c2.execute("SELECT COUNT(*) FROM domains WHERE name = ?", (d,))
        if c2.fetchone()[0] == 0:
            remaining_non_canon += cnt
    print(f"Experiments still in non-canonical domains: {remaining_non_canon}")

    conn.commit()
    conn.close()

    print("\nDONE")


if __name__ == "__main__":
    main()
