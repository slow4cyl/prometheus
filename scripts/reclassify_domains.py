#!/usr/bin/env python3
"""
reclassify_domains.py — Automatic domain reclassification pipeline.

Runs after each cycle to:
1. Find experiments in domains with < MIN_DOMAIN_SIZE experiments and reclassify them
2. Run consistency scan to find neighbors of reclassified experiments
3. Detect naming variants that should be merged

Fully automatic — no advisory, no human-in-the-loop.

Usage:
    python3 reclassify_domains.py              # Full run
    python3 reclassify_domains.py --dry-run    # Show what would change
    python3 reclassify_domains.py --since 24h  # Only experiments from last 24h
    python3 reclassify_domains.py --orphans-only  # Only fix orphan domains
"""

import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import sqlite3
from db_retry import get_db as retry_get_db
import sys
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

# ── Paths ──
DB_PATH = _PP_PROMETHEUS_DB
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.expanduser("~/.hermes/classifier/reclassify_log.jsonl")

# ── Thresholds ──
MIN_DOMAIN_SIZE = 10           # Domains with fewer experiments get reclassified
RECLASSIFY_THRESHOLD = 0.38    # Minimum embedding similarity to assign a domain
CONSISTENCY_SIMILARITY = 0.60  # Cosine similarity for neighbor finding

# Import the embedding classifier
sys.path.insert(0, SCRIPT_DIR)
from embedding_domain_classifier import (
    DOMAIN_DESCRIPTIONS,
    get_embedding,
    get_embeddings_batch,
    load_domain_embeddings,
    cosine_similarity,
)

# Embedding classifier's canonical set (for best-match lookup)
# Includes both hard-coded DOMAIN_DESCRIPTIONS and auto-generated descriptions
# from domain_auto_descriptions.json (created by domain_merge_sync.py cron).
# Without auto-generated domains, experiments matching those centroids are
# rejected as non-canonical and stuck in "unknown" forever.
EMBEDDING_CANONICAL = set(DOMAIN_DESCRIPTIONS.keys()) - {"general"}
try:
    from embedding_domain_classifier import load_auto_descriptions
    _auto_descs = load_auto_descriptions()
    EMBEDDING_CANONICAL |= set(_auto_descs.keys())
except Exception:
    pass


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def _is_malformed_concatenation(domain: str) -> bool:
    """Check if a domain name is a separator-less concatenation of 2+ known
    domain labels (the classifier's delimiter-loss bug).

    Delegates to domain_creation_gate.decompose_concatenation(). Returns True
    if the domain decomposes into >=2 distinct known labels, False otherwise.

    This guard prevents garbage domains (e.g. 'biologyimmunologyreliability')
    from being treated as canonical — which would cause the reclassify pipeline
    to reclassify OTHER experiments INTO them, spreading contamination.
    """
    try:
        from domain_creation_gate import decompose_concatenation
        parts = decompose_concatenation(domain)
        return parts is not None and len(parts) >= 2
    except Exception:
        return False


def get_database_canonical_domains(conn, min_size=MIN_DOMAIN_SIZE):
    """
    Derive canonical domains from the database + embedding classifier.

    A domain is canonical if ANY of:
    1. It has >= min_size experiments in the database
    2. It exists in the embedding classifier's DOMAIN_DESCRIPTIONS

    This preserves legitimate small domains (e.g., biophysics: 6, linguistics: 7)
    while catching true orphans (e.g., AI_calibration, LLM capacity limits).

    MALFORMED concatenation guard: separator-less glue-ups (e.g.
    'biologyimmunologyreliability') that crossed the >=10 threshold and became
    "canonical" are excluded here so the reclassify pipeline treats them as
    orphans and reclassifies their experiments to real domains instead of
    reclassifying INTO them.
    """
    # Start with embedding classifier's known domains
    canonical = set(EMBEDDING_CANONICAL)

    # Add database domains with enough experiments
    cursor = conn.execute(
        "SELECT domain, COUNT(*) as cnt FROM experiments "
        "WHERE domain != '' AND domain IS NOT NULL "
        "GROUP BY domain ORDER BY cnt DESC"
    )
    all_domains = {}
    for row in cursor:
        all_domains[row["domain"]] = row["cnt"]
        if row["cnt"] >= min_size:
            canonical.add(row["domain"])

    # Purge malformed concatenations that snuck into the canonical set
    # (the delimiter-loss bug can produce labels that accumulate >=10 experiments
    # and self-promote to "canonical" before the domain_creation_gate catches them)
    malformed_in_canonical = {d for d in canonical if _is_malformed_concatenation(d)}
    if malformed_in_canonical:
        for d in malformed_in_canonical:
            canonical.discard(d)
            print(f"  [guard] excluded malformed domain from canonical set: {d!r}")

    # Purge methodology labels that aren't real application domains.
    # Labels like 'tfidf' describe a technique (TF-IDF vectorization), not a
    # research domain. They accumulate experiments from many different domains
    # (injection_detection, adversarial_ml, fraud_detection, nlp, etc.) and
    # become canonical by volume, preventing the reclassifier from redistributing
    # those experiments to their correct application domains.
    METHODOLOGY_LABELS = {'tfidf'}
    methodology_in_canonical = {d for d in canonical if d in METHODOLOGY_LABELS}
    if methodology_in_canonical:
        for d in methodology_in_canonical:
            canonical.discard(d)
            print(f"  [guard] excluded methodology label from canonical set: {d!r}")

    # Purge "unknown" — it is not a real domain. It is the default value
    # written by result_bridge.py when no domain metadata is present, and it
    # accumulates by volume (thousands of unclassified experiments). Without
    # this purge, "unknown" self-promotes to canonical status via the
    # >=min_size threshold and becomes immune to reclassification, trapping
    # thousands of experiments in a ghost domain forever.
    if "unknown" in canonical:
        canonical.discard("unknown")
        print(f"  [guard] excluded 'unknown' from canonical set (not a real domain)")

    return canonical, all_domains


def classify_with_top2(text: str) -> Tuple[str, float, str, float]:
    """
    Classify text using embedding similarity against all known domains.
    Returns: (domain1, score1, domain2, score2)
    """
    domain_embs = load_domain_embeddings()
    if not domain_embs:
        return ("general", 0.0, "general", 0.0)
    
    emb = get_embedding(text)
    if emb is None:
        return ("general", 0.0, "general", 0.0)
    
    scores = []
    for domain, d_emb in domain_embs.items():
        if domain == "general":
            continue
        sim = cosine_similarity(emb, d_emb)
        scores.append((domain, sim))
    
    scores.sort(key=lambda x: x[1], reverse=True)
    
    if len(scores) >= 2:
        return (scores[0][0], scores[0][1], scores[1][0], scores[1][1])
    elif scores:
        return (scores[0][0], scores[0][1], "general", 0.0)
    return ("general", 0.0, "general", 0.0)


def build_text(exp):
    """Build classification text from experiment fields."""
    parts = []
    if exp.get("hypothesis"):
        parts.append(exp["hypothesis"][:300])
    if exp.get("result"):
        parts.append(exp["result"][:300])
    return " ".join(parts)


def map_to_canonical(domain, canonical_domains):
    """
    Map a non-canonical domain name to the best canonical match.
    Uses token matching with preference for shorter (more general) domains.
    """
    domain_tokens = set(domain.lower().replace("_", " ").replace("-", " ").split())
    
    # Find all canonical domains that share at least 1 token
    candidates = []
    for canon in canonical_domains:
        canon_tokens = set(canon.lower().replace("_", " ").replace("-", " ").split())
        overlap = len(domain_tokens & canon_tokens)
        if overlap >= 1:
            candidates.append((canon, overlap))
    
    if not candidates:
        return None
    
    # Sort by: (1) number of shared tokens (more = better), (2) shorter name (more general)
    candidates.sort(key=lambda x: (-x[1], len(x[0])))
    return candidates[0][0]


def find_and_reclassify_orphans(conn, canonical_domains, since_hours=None, dry_run=False):
    """
    Find experiments in non-canonical domains and reclassify them.
    Returns (reclassifications, orphan_info).
    
    Strategy:
    1. Embedding classification as primary (uses full text context)
    2. If embedding confidence is high AND top-2 are close → use string fallback
       (ambiguous text means domain name is more reliable signal)
    3. String-based mapping as fallback for domains not in embeddings
    """
    query = "SELECT id, hypothesis, result, domain FROM experiments WHERE domain != '' AND domain IS NOT NULL"
    params = []
    
    if since_hours:
        cutoff = time.time() - (since_hours * 3600)
        query += " AND created_at > ?"
        params.append(cutoff)
    
    cursor = conn.execute(query, params)
    orphans = []
    domain_counts = Counter()
    
    for row in cursor:
        domain = row["domain"]
        if domain not in canonical_domains:
            orphans.append(dict(row))
            domain_counts[domain] += 1
    
    if not orphans:
        return [], {}
    
    print(f"  Found {len(orphans)} orphan experiments in {len(domain_counts)} non-canonical domains")
    for domain, count in domain_counts.most_common(20):
        print(f"    {domain}: {count}")
    
    # Reclassify each orphan
    reclassifications = []
    
    for exp in orphans:
        old_domain = exp["domain"]
        text = build_text(exp)
        
        # Get embedding classification
        emb_domain, emb_score = ("general", 0.0)
        emb_alt, emb_alt_score = ("general", 0.0)
        if text.strip():
            emb_domain, emb_score, emb_alt, emb_alt_score = classify_with_top2(text)

        # MALFORMED concatenation guard: reject garbage domains from the
        # embedding classifier's top-1 and alt results. The embedding cache
        # may still contain stale glue-up labels (e.g.
        # 'biologyimmunologyreliability') even after the canonical-set purge
        # in get_database_canonical_domains(). If the classifier matches an
        # experiment to one of these, we must NOT reclassify INTO it.
        if _is_malformed_concatenation(emb_domain):
            emb_domain, emb_score = ("general", 0.0)
        if _is_malformed_concatenation(emb_alt):
            emb_alt, emb_alt_score = ("general", 0.0)
        
        # Get string-based classification
        str_domain = map_to_canonical(old_domain, canonical_domains)
        
        # Decision logic:
        # 1. If embedding is confident AND string agrees or is None → use embedding
        # 2. If embedding is confident AND string disagrees AND scores are close → use string
        #    (domain name is a more reliable signal when text is ambiguous)
        # 3. If no embedding → use string
        
        if emb_domain in canonical_domains and emb_score >= 0.50:
            # Embedding is confident — check if string also agrees
            if str_domain is None or str_domain == emb_domain:
                reclassifications.append((exp["id"], old_domain, emb_domain, emb_score, emb_alt, emb_alt_score))
            elif str_domain and str_domain != emb_domain:
                # Embedding and string disagree — if embedding margin is thin, prefer string
                margin = emb_score - (emb_alt_score or 0)
                if margin < 0.15:
                    reclassifications.append((exp["id"], old_domain, str_domain, 0.8, emb_domain, emb_score))
                else:
                    reclassifications.append((exp["id"], old_domain, emb_domain, emb_score, str_domain, 0.8))
            else:
                reclassifications.append((exp["id"], old_domain, emb_domain, emb_score, emb_alt, emb_alt_score))
        elif str_domain:
            # Embedding is weak or unknown — use string match
            reclassifications.append((exp["id"], old_domain, str_domain, 0.8, emb_domain, emb_score))
        elif emb_domain in canonical_domains and emb_score >= RECLASSIFY_THRESHOLD:
            # Weak embedding but above minimum threshold
            reclassifications.append((exp["id"], old_domain, emb_domain, emb_score, emb_alt, emb_alt_score))
    
    return reclassifications, dict(domain_counts)


def consistency_scan(conn, reclassified, canonical_domains, dry_run=False):
    """
    After reclassifying experiments, find similar experiments in other domains
    that might also be misclassified.
    
    Uses word-overlap indexing for speed.
    """
    if not reclassified:
        return []
    
    # Build word index from all experiments (titles only)
    cursor = conn.execute(
        "SELECT id, hypothesis, domain FROM experiments "
        "WHERE hypothesis IS NOT NULL AND hypothesis != '' "
        "AND domain IN ({})".format(",".join("?" for _ in canonical_domains)),
        list(canonical_domains)
    )
    all_exps = [dict(row) for row in cursor]
    
    STOP_WORDS = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
        'on', 'with', 'at', 'by', 'from', 'as', 'into', 'through', 'during',
        'and', 'but', 'or', 'nor', 'not', 'so', 'yet', 'both', 'either',
        'this', 'that', 'these', 'those', 'it', 'its', 'they', 'them',
        'also', 'using', 'used', 'use', 'based', 'via', 'show', 'shows',
        'effect', 'results', 'experiment', 'test', 'model', 'study',
    }
    
    word_index = {}
    for exp in all_exps:
        title = (exp.get("hypothesis") or "").lower()
        words = set()
        for word in title.split():
            word = word.strip('.,;:!?()[]{}"\'-')
            if len(word) >= 4 and word not in STOP_WORDS:
                words.add(word)
        for word in words:
            if word not in word_index:
                word_index[word] = []
            word_index[word].append((exp["id"], exp["domain"]))
    
    domain_embs = load_domain_embeddings()
    if not domain_embs:
        return []
    
    neighbors = []
    checked = set()
    
    for exp_id, old_domain, new_domain, confidence, _, _ in reclassified:
        title_row = conn.execute(
            "SELECT hypothesis FROM experiments WHERE id = ?", (exp_id,)
        ).fetchone()
        if not title_row or not title_row["hypothesis"]:
            continue
        
        title = title_row["hypothesis"]
        title_emb = get_embedding(title)
        if title_emb is None:
            continue
        
        # Find candidates via word overlap
        title_words = set()
        for word in title.lower().split():
            word = word.strip('.,;:!?()[]{}"\'-')
            if len(word) >= 4 and word not in STOP_WORDS:
                title_words.add(word)
        
        candidate_ids = set()
        for word in title_words:
            if word in word_index:
                for cand_id, cand_domain in word_index[word]:
                    if cand_id != exp_id and cand_domain != new_domain:
                        candidate_ids.add((cand_id, cand_domain))
        
        if len(candidate_ids) > 30:
            candidate_ids = set(list(candidate_ids)[:30])
        
        if not candidate_ids:
            continue
        
        ids_only = [c[0] for c in candidate_ids]
        placeholders = ",".join("?" for _ in ids_only)
        cursor = conn.execute(
            f"SELECT id, hypothesis, domain FROM experiments WHERE id IN ({placeholders})",
            ids_only
        )
        candidates = [dict(row) for row in cursor]
        
        cand_texts = [c.get("hypothesis", "")[:300] for c in candidates]
        cand_embs = get_embeddings_batch(cand_texts)
        
        for cand, cand_emb in zip(candidates, cand_embs):
            if cand_emb is None:
                continue
            
            sim = cosine_similarity(title_emb, cand_emb)
            if sim >= CONSISTENCY_SIMILARITY:
                cand_text = build_text(cand)
                if not cand_text.strip():
                    continue
                
                d1, s1, d2, s2 = classify_with_top2(cand_text)
                if d1 in canonical_domains and d1 == new_domain and s1 >= RECLASSIFY_THRESHOLD:
                    key = (cand["id"], cand["domain"], new_domain)
                    if key not in checked:
                        checked.add(key)
                        neighbors.append((cand["id"], cand["domain"], new_domain, s1, d2, s2))
    
    return neighbors


def apply_reclassifications(conn, reclassifications, dry_run=False):
    """Apply domain reclassifications to the database."""
    applied = 0
    for rec in reclassifications:
        exp_id, old_domain, new_domain, confidence, alt_domain, alt_confidence = rec
        
        if old_domain == new_domain:
            continue
        
        if not dry_run:
            conn.execute(
                "UPDATE experiments SET domain = ? WHERE id = ?",
                (new_domain, exp_id)
            )
        
        applied += 1
    
    if not dry_run:
        conn.commit()
    
    return applied


def log_reclassification(reclassifications):
    """Log reclassifications to JSONL for auditing."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    
    with open(LOG_PATH, "a") as f:
        for rec in reclassifications:
            exp_id, old_domain, new_domain, confidence, alt_domain, alt_confidence = rec
            entry = {
                "timestamp": time.time(),
                "exp_id": exp_id,
                "old_domain": old_domain,
                "new_domain": new_domain,
                "confidence": round(confidence, 4),
                "alt_domain": alt_domain,
                "alt_confidence": round(alt_confidence, 4),
            }
            f.write(json.dumps(entry) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Automatic domain reclassification pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying DB")
    parser.add_argument("--since", help="Only process experiments from last N hours (e.g. '24h', '7d')")
    parser.add_argument("--skip-consistency", action="store_true", help="Skip consistency scan")
    parser.add_argument("--orphans-only", action="store_true", help="Only fix orphan domains, skip consistency")
    parser.add_argument("--min-domain-size", type=int, default=MIN_DOMAIN_SIZE,
                        help=f"Minimum experiments for a domain to be canonical (default: {MIN_DOMAIN_SIZE})")
    parser.add_argument("--verbose", action="store_true", help="Print detailed output")
    args = parser.parse_args()
    
    # Parse --since
    since_hours = None
    if args.since:
        if args.since.endswith("h"):
            since_hours = int(args.since[:-1])
        elif args.since.endswith("d"):
            since_hours = int(args.since[:-1]) * 24
    
    conn = get_db()
    
    print("=" * 60)
    print("DOMAIN RECLASSIFICATION PIPELINE")
    print("=" * 60)
    
    # Derive canonical domains from database
    canonical_domains, all_domain_counts = get_database_canonical_domains(conn, args.min_domain_size)
    print(f"\nCanonical domains ({len(canonical_domains)}, min size {args.min_domain_size}):")
    for domain in sorted(canonical_domains):
        cnt = all_domain_counts.get(domain, 0)
        marker = " (from embeddings)" if cnt == 0 else ""
        print(f"  {domain}: {cnt}{marker}")
    
    non_canonical = {d: c for d, c in all_domain_counts.items() if d not in canonical_domains}
    if non_canonical:
        print(f"\nNon-canonical domains ({len(non_canonical)}):")
        for domain, count in sorted(non_canonical.items(), key=lambda x: -x[1]):
            print(f"  {domain}: {count}")
    
    # ── Step 1: Find and reclassify orphan experiments ──
    print(f"\n[1/3] Finding orphan experiments (domains with <{args.min_domain_size} experiments)...")
    orphan_reclassifications, orphan_info = find_and_reclassify_orphans(
        conn, canonical_domains, since_hours, args.dry_run
    )
    
    successful_orphans = [r for r in orphan_reclassifications if r[1] != r[2]]
    print(f"\n  {len(successful_orphans)} experiments will be reclassified")
    
    if args.verbose and successful_orphans:
        for rec in successful_orphans[:30]:
            exp_id, old, new, conf, alt, alt_conf = rec
            print(f"    {exp_id}: {old} → {new} (conf: {conf:.3f})")
    
    # ── Step 2: Consistency scan ──
    consistency_reclassifications = []
    if not args.skip_consistency and not args.orphans_only:
        print("\n[2/3] Running consistency scan...")
        consistency_reclassifications = consistency_scan(
            conn, successful_orphans, canonical_domains, args.dry_run
        )
        print(f"  Found {len(consistency_reclassifications)} neighbors to reclassify")
        
        if args.verbose and consistency_reclassifications:
            for rec in consistency_reclassifications[:20]:
                exp_id, old, new, conf, alt, alt_conf = rec
                print(f"    {exp_id}: {old} → {new} (conf: {conf:.3f})")
    else:
        print("\n[2/3] Consistency scan skipped")
    
    # ── Step 3: Apply all reclassifications ──
    all_reclassifications = successful_orphans + consistency_reclassifications
    seen = set()
    deduped = []
    for rec in all_reclassifications:
        if rec[0] not in seen:
            seen.add(rec[0])
            deduped.append(rec)
    all_reclassifications = deduped
    
    print(f"\n[3/3] Applying {len(all_reclassifications)} reclassifications...")
    applied = apply_reclassifications(conn, all_reclassifications, args.dry_run)
    
    if args.dry_run:
        print(f"  DRY RUN — would apply {applied} reclassifications")
    else:
        print(f"  Applied {applied} reclassifications")
        if applied > 0:
            log_reclassification(all_reclassifications)
    
    # ── Summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Canonical domains: {len(canonical_domains)}")
    print(f"  Non-canonical domains: {len(non_canonical)}")
    print(f"  Orphans found: {len(orphan_reclassifications)}")
    print(f"  Orphans reclassified: {len(successful_orphans)}")
    print(f"  Consistency neighbors: {len(consistency_reclassifications)}")
    print(f"  Total reclassifications: {applied}")
    print(f"  Dry run: {args.dry_run}")
    print("=" * 60)
    
    return {
        "canonical_domains": len(canonical_domains),
        "orphans_found": len(orphan_reclassifications),
        "orphans_reclassified": len(successful_orphans),
        "consistency_neighbors": len(consistency_reclassifications),
        "total_applied": applied,
        "dry_run": args.dry_run,
    }


if __name__ == "__main__":
    result = main()
    print(json.dumps(result))
