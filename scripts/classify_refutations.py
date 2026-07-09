#!/usr/bin/env python3
"""
classify_refutations.py — Epistemic routing classifier for Prometheus experiments.

Classifies each experiment's refutation_type based on:
  1. How many prior experiments tested the same hypothesis (approximated via keyword overlap)
  2. Whether all prior tests agreed (consistency)
  3. The magnitude of confidence_change (effect size)
  4. Whether the result indicates opposite direction to hypothesis
  5. Whether predicted_direction and observed_direction are available

Classification types:
  SUPPORTED    — hypothesis confirmed
  MECHANISTIC  — refuted with strong evidence across independent tests
  STATISTICAL  — refuted but result is marginal/noisy
  BOUNDARY     — mixed results (works sometimes, not always)
  UNCERTAIN    — insufficient data to classify

Usage:
  python3 classify_refutations.py              # Classify all unclassified experiments
  python3 classify_refutations.py --exp exp_5032  # Classify a specific experiment
  python3 classify_refutations.py --report     # Show classification distribution
  python3 classify_refutations.py --backfill   # Re-classify ALL experiments (not just unclassified)

The classifier is deterministic and stateless — it reads the experiments table
and writes refutation_type back. No external dependencies.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from difflib import SequenceMatcher

# Canonical main hermes dir — derived from script location to avoid HOME override issues

"""CLI tool: Classify Refutations.

Usage: python3 classify_refutations.py [options]
"""

_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(os.path.join(_hermes_home_env, "prometheus_db.py")) else _HERMES_MAIN

sys.path.insert(0, HERMES_HOME)
from prometheus_db import get_db


DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")

# --- Hypothesis similarity (keyword Jaccard, matching batch_create_tasks.py approach) ---

def extract_keywords(text):
    """Extract meaningful keywords from hypothesis text."""
    if not text:
        return set()
    # Lowercase, remove experiment IDs, split on non-alpha
    text = text.lower()
    text = re.sub(r'exp_\d+', '', text)
    words = set(re.findall(r'[a-z_]{3,}', text))
    # Remove common filler words
    stop = {'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had',
            'her', 'was', 'one', 'our', 'out', 'has', 'his', 'how', 'its', 'may',
            'new', 'now', 'old', 'see', 'way', 'who', 'why', 'did', 'get', 'let',
            'say', 'she', 'too', 'use', 'that', 'with', 'have', 'this', 'will',
            'your', 'from', 'they', 'been', 'said', 'each', 'make', 'like', 'long',
            'look', 'many', 'most', 'over', 'such', 'take', 'than', 'them', 'then',
            'these', 'what', 'when', 'more', 'some', 'time', 'very', 'just', 'also',
            'into', 'only', 'than', 'does', 'after', 'about', 'their', 'there',
            'which', 'would', 'could', 'should', 'being', 'between', 'other',
            'where', 'while', 'those', 'using', 'based', 'both', 'first', 'found',
            'shows', 'tested', 'tested', 'tested', 'already', 'answered',
            'confirmed', 'refuted', 'duplicate', 'hypothesis'}
    return words - stop


def hypothesis_overlap(text1, text2):
    """Compute Jaccard overlap between two hypothesis texts."""
    kw1 = extract_keywords(text1)
    kw2 = extract_keywords(text2)
    if not kw1 or not kw2:
        return 0.0
    intersection = kw1 & kw2
    union = kw1 | kw2
    return len(intersection) / len(union) if union else 0.0


def find_related_experiments(conn, experiment_id, hypothesis, threshold=0.35):
    """Find prior experiments with similar hypotheses."""
    if not hypothesis:
        return []

    # Get recent experiments (last 500 for performance)
    rows = conn.execute("""
        SELECT id, hypothesis, result, tags, confidence_change,
               predicted_direction, observed_direction, design_vector
        FROM experiments
        WHERE id != ? AND status = 'completed'
        ORDER BY created_at DESC
        LIMIT 500
    """, (experiment_id,)).fetchall()

    related = []
    for row in rows:
        overlap = hypothesis_overlap(hypothesis, row[1] or "")
        if overlap >= threshold:
            related.append({
                "id": row[0],
                "hypothesis": row[1],
                "result": row[2],
                "tags": row[3],
                "confidence_change": row[4],
                "predicted_direction": row[5],
                "observed_direction": row[6],
                "design_vector": row[7],
                "overlap": overlap,
            })

    # Sort by overlap descending
    related.sort(key=lambda x: x["overlap"], reverse=True)
    return related


def parse_tags(tags_str):
    """Parse tags JSON array."""
    if not tags_str:
        return []
    try:
        t = json.loads(tags_str)
        return t if isinstance(t, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def result_direction(result_text):
    """Extract the directional signal from a result string.
    Returns: +1 (confirmed/supported), -1 (refuted), 0 (ambiguous/partial)
    """
    if not result_text:
        return 0
    upper = result_text.upper()

    # Strong refutation signals
    if any(kw in upper for kw in ["REFUTED", "NOT SUPPORTED", "NOT CONFIRMED",
                                    "DOES NOT", "DOESN'T", "FAILED TO"]):
        # But check for partial/nuanced results
        if any(kw in upper for kw in ["PARTIAL", "PARTIALLY", "NUANCED"]):
            return 0
        return -1

    # Strong support signals
    if any(kw in upper for kw in ["CONFIRMED", "SUPPORTED", "SUPPORTS",
                                    "DOES APPLY", "IS REAL", "WORKS"]):
        return 1

    # Already answered / duplicate — these don't provide new directional info
    if any(kw in upper for kw in ["ALREADY ANSWERED", "DUPLICATE"]):
        return 0

    return 0


def classify_single(conn, exp_id):
    """Classify a single experiment's refutation_type.

    Returns the classification string.
    """
    row = conn.execute("""
        SELECT hypothesis, result, tags, confidence_change, status,
               predicted_direction, observed_direction, design_vector
        FROM experiments WHERE id = ?
    """, (exp_id,)).fetchone()

    if not row:
        return None

    hypothesis, result, tags_str, conf_change, status = row[:5]
    pred_dir, obs_dir, design_vec = row[5], row[6], row[7]

    # If experiment isn't completed, keep as UNCERTAIN
    if status != "completed":
        return "UNCERTAIN"

    tags = parse_tags(tags_str)
    tags_upper = [t.upper() for t in tags]

    # --- Determine directional signal: TAGS are authoritative, result text is fallback ---
    # Tags are set by the worker who ran the experiment and know the ground truth.
    # Result text is free-form and can contain "DOES NOT" in non-refutation contexts
    # (e.g., "it doesn't matter what the model is" is not a refutation).
    dir_signal = 0

    # Check tags first (authoritative)
    if any(t in ["CONFIRMED", "SUPPORTED", "BREAKTHROUGH"] for t in tags_upper):
        dir_signal = 1
    elif any(t in ["REFUTED"] for t in tags_upper):
        dir_signal = -1
    elif any(t in ["PARTIAL", "PARTIAL_SUPPORT", "PARTIALLY_SUPPORTED"] for t in tags_upper):
        dir_signal = 0  # Ambiguous — boundary territory

    # Fall back to result text only if tags don't have a clear signal
    if dir_signal == 0:
        dir_signal = result_direction(result)

    # --- Rule 1: SUPPORTED experiments ---
    if dir_signal > 0:
        return "SUPPORTED"

    # --- Rule 2: Already answered / duplicate ---
    if any(t in ["DUPLICATE", "ALREADY_ANSWERED", "ALREADY ANSWERED"] for t in tags_upper):
        if dir_signal > 0:
            return "SUPPORTED"
        elif dir_signal < 0:
            return "STATISTICAL"
        return "STATISTICAL"

    # --- Rule 3: No refutation signal → UNCERTAIN ---
    if dir_signal == 0:
        return "UNCERTAIN"

    # --- From here: dir_signal < 0 (REFUTED) ---
    # Find related prior experiments on the same hypothesis
    related = find_related_experiments(conn, exp_id, hypothesis)

    # Filter to only refutation results (not supported duplicates)
    related_refutations = [r for r in related if result_direction(r["result"]) < 0]
    related_supports = [r for r in related if result_direction(r["result"]) > 0]

    # --- Rule 4: First experiment, refuted → UNCERTAIN ---
    # Don't burn on N=1
    if len(related) == 0:
        # Use magnitude as a heuristic
        try:
            abs_conf = abs(float(conf_change)) if conf_change else 0
        except (TypeError, ValueError):
            abs_conf = 0
        if abs_conf > 0.05:
            # Strong effect but N=1 — still UNCERTAIN but flag for follow-up
            return "UNCERTAIN"
        elif abs_conf < 0.01:
            return "STATISTICAL"
        else:
            return "UNCERTAIN"

    # --- Rule 5: Multiple experiments, all agree on refutation ---
    total_related = len(related)
    n_refutations = len(related_refutations)
    n_supports = len(related_supports)

    # Check consistency across related experiments
    if n_refutations >= 2 and n_supports == 0:
        # All related experiments agree: refuted
        # Check design diversity — are the tests genuinely different?
        designs = [r.get("design_vector") for r in related if r.get("design_vector")]
        unique_designs = len(set(designs))

        # Check if confidence changes are consistently negative
        conf_changes = []
        for r in related_refutations:
            try:
                conf_changes.append(float(r["confidence_change"]))
            except (TypeError, ValueError):
                pass
        avg_conf = sum(conf_changes) / len(conf_changes) if conf_changes else 0

        if total_related >= 3:
            # Strong evidence: 3+ independent refutations
            return "MECHANISTIC"
        elif total_related == 2:
            # Moderate evidence: 2 refutations
            if avg_conf < -0.02:
                return "MECHANISTIC"
            else:
                return "STATISTICAL"
        else:
            return "STATISTICAL"

    # --- Rule 6: Mixed results → BOUNDARY ---
    if n_refutations > 0 and n_supports > 0:
        return "BOUNDARY"

    # --- Rule 7: Related experiments exist but all are duplicates/informational ---
    if n_refutations == 0 and n_supports == 0:
        # Related experiments exist but none provide clear directional signal
        # This is likely a thread with many exploratory experiments
        try:
            abs_conf = abs(float(conf_change)) if conf_change else 0
        except (TypeError, ValueError):
            abs_conf = 0
        if abs_conf > 0.03:
            return "UNCERTAIN"  # Needs more data
        else:
            return "STATISTICAL"

    # --- Rule 8: All supports, but current is refuted → BOUNDARY ---
    if n_supports > 0 and dir_signal < 0:
        return "BOUNDARY"

    # Default: UNCERTAIN
    return "UNCERTAIN"


def classify_all(backfill=False):
    """Classify all experiments (or just unclassified ones)."""
    with get_db() as conn:
        if backfill:
            rows = conn.execute("""
                SELECT id FROM experiments WHERE status = 'completed' AND id GLOB 'exp_[0-9]*'
                ORDER BY created_at DESC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT id FROM experiments
                WHERE status = 'completed' AND refutation_type = 'UNCERTAIN'
                  AND id GLOB 'exp_[0-9]*'
                ORDER BY created_at DESC
            """).fetchall()

        if not rows:
            print("No experiments to classify.")
            return

        print(f"Classifying {len(rows)} experiments...")
        classified = Counter()

        for (exp_id,) in rows:
            # For backfill, re-read from DB each time (previous classifications affect related lookups)
            # But for efficiency, classify in reverse chronological order
            pass

        # Process in reverse chronological order (newest first)
        # so that newer experiments can reference older ones
        for (exp_id,) in rows:
            old_type = conn.execute(
                "SELECT refutation_type FROM experiments WHERE id = ?", (exp_id,)
            ).fetchone()
            old_type = old_type[0] if old_type else "UNCERTAIN"

            new_type = classify_single(conn, exp_id)
            if new_type and new_type != old_type:
                conn.execute(
                    "UPDATE experiments SET refutation_type = ? WHERE id = ?",
                    (new_type, exp_id)
                )
                classified[new_type] += 1

        print(f"\nClassification results:")
        for typ, count in sorted(classified.items(), key=lambda x: -x[1]):
            print(f"  {typ}: {count}")
        print(f"  Total changed: {sum(classified.values())}")


def report():
    """Show classification distribution."""
    with get_db() as conn:
        # Overall distribution
        rows = conn.execute("""
            SELECT refutation_type, COUNT(*) as cnt
            FROM experiments
            WHERE status = 'completed' AND id GLOB 'exp_[0-9]*'
            GROUP BY refutation_type
            ORDER BY cnt DESC
        """).fetchall()

        print("=== Refutation Type Distribution ===")
        total = 0
        for typ, cnt in rows:
            print(f"  {typ or 'NULL'}: {cnt}")
            total += cnt
        print(f"  Total: {total}")

        # Recent (last 24h)
        import time
        cutoff = time.time() - 86400
        rows = conn.execute("""
            SELECT refutation_type, COUNT(*) as cnt
            FROM experiments
            WHERE status = 'completed' AND created_at > ? AND id GLOB 'exp_[0-9]*'
            GROUP BY refutation_type
            ORDER BY cnt DESC
        """, (cutoff,)).fetchall()

        print(f"\n=== Last 24h ===")
        total24 = 0
        for typ, cnt in rows:
            print(f"  {typ or 'NULL'}: {cnt}")
            total24 += cnt
        print(f"  Total: {total24}")

        # Top hypotheses by related experiment count
        print(f"\n=== Most-Tested Hypotheses (top 10 by related experiments) ===")
        rows = conn.execute("""
            SELECT hypothesis, cnt FROM (
                SELECT hypothesis, COUNT(*) as cnt
                FROM experiments
                WHERE status = 'completed' AND id GLOB 'exp_[0-9]*'
                GROUP BY LOWER(hypothesis)
                HAVING cnt > 1
                ORDER BY cnt DESC
                LIMIT 10
            )
        """).fetchall()
        for hyp, cnt in rows:
            print(f"  [{cnt}x] {(hyp or '')[:100]}")


def main():
    parser = argparse.ArgumentParser(description="Classify experiment refutation types")
    parser.add_argument("--exp", help="Classify a specific experiment")
    parser.add_argument("--report", action="store_true", help="Show classification distribution")
    parser.add_argument("--backfill", action="store_true", help="Re-classify ALL experiments")
    args = parser.parse_args()

    if args.report:
        report()
    elif args.exp:
        with get_db() as conn:
            old = conn.execute("SELECT refutation_type FROM experiments WHERE id = ?", (args.exp,)).fetchone()
            old = old[0] if old else "NOT FOUND"
            new = classify_single(conn, args.exp)
            conn.execute("UPDATE experiments SET refutation_type = ? WHERE id = ?", (new, args.exp))
            print(f"{args.exp}: {old} -> {new}")
    else:
        classify_all(backfill=args.backfill)


if __name__ == "__main__":
    main()
