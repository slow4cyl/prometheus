#!/usr/bin/env python3
"""
queue_entropy_monitor.py — Monitor queue entropy and inject diversity when needed.

Based on exp_72526580: queue entropy declining at ~0.01 bits/cycle.
NEW_FROM_SYNTHESIS has ZERO entropy — synthesis drives homogeneity.
Stagnation threshold: H < 2.0 bits (~4 effective threads).

This script:
1. Computes current queue entropy (Shannon entropy over thread distribution)
2. Alerts when entropy drops below threshold
3. Injects diversity items from under-represented threads when needed

Usage:
    python3 queue_entropy_monitor.py              # Report only
    python3 queue_entropy_monitor.py --inject     # Inject diversity if needed
    python3 queue_entropy_monitor.py --force       # Force inject regardless of threshold
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from db_retry import get_db
import time
from collections import Counter

"""CLI tool: Queue Entropy Monitor.

Usage: python3 queue_entropy_monitor.py [options]
"""


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from curiosity_scorer import classify_thread, load_state, is_already_in_queue

SELF_STATE_PATH = os.path.expanduser("~/.hermes/self_state.json")
PROMETHEUS_DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")
ENTROPY_THRESHOLD = 2.0  # Below this = stagnation danger
ENTROPY_TARGET = 3.0     # Target entropy for healthy queue

# Diversity injection templates — questions from under-represented threads
DIVERSITY_INJECTIONS = {
    "cross_lingual": [
        "What is the minimum training samples per language for cross-lingual text classification F1>0.8?",
        "Can character n-gram overlap between languages predict cross-lingual transfer performance?",
        "Does code-mixed (EN+ZH) input bypass monolingual text classifiers?",
    ],
    "hardware": [
        "What is the minimum edge hardware for real-time text classification at 100ms latency?",
        "How does INT4 quantization affect classifier accuracy vs INT8 on ARM processors?",
        "Can SIMD-optimized cosine similarity replace embedding models at the edge?",
    ],
    "financial_fraud_detection": [
        "Can text classification features transfer to credit card fraud detection?",
        "What is the information density of fraud detection experiments vs text classification?",
        "Is Isolation Forest or Local Outlier Factor better for financial anomaly detection?",
    ],
    "audio_security": [
        "Can audio adversarial detection features transfer to text classification?",
        "What is the cross-modal transfer rate between audio and text adversarial detection?",
        "Does spectral analysis of audio adversarial samples generalize to text perturbations?",
    ],
    "medical_nlp": [
        "Can text classification transfer to medical NLP de-identification?",
        "What is the minimum F1 for medical text classification using transfer learning?",
        "Does clinical note anonymization benefit from adversarial robustness training?",
    ],
    "ensemble_methods": [
        "Does ensemble diversity improve when classifiers are trained on different languages?",
        "What is the optimal ensemble size for cross-domain detection (3, 5, 7, 10)?",
        "Can ensemble disagreement detect out-of-distribution inputs?",
    ],
    "calibration": [
        "Does calibration improve when trained on multi-language classification data?",
        "What is the temperature scaling difference between English and Chinese classifiers?",
        "Is Platt scaling or isotonic regression better for multi-domain calibration?",
    ],
}


def compute_queue_entropy(queue):
    """Compute Shannon entropy of thread distribution in queue."""
    threads = [classify_thread(str(item) if not isinstance(item, dict) else item.get("text", item.get("question", str(item)))) for item in queue]
    freq = Counter(threads)
    total = len(threads)
    if total == 0:
        return 0.0, Counter(), threads
    entropy = -sum((c / total) * math.log2(c / total) for c in freq.values() if c > 0)
    return entropy, freq, threads


def get_underrepresented_threads(freq, min_count=3):
    """Find threads with fewer than min_count items in the queue."""
    underrep = []
    for thread in DIVERSITY_INJECTIONS:
        if freq.get(thread, 0) < min_count:
            underrep.append(thread)
    return underrep


def is_duplicate_in_db(text):
    """Check if an item already exists in prometheus.db curiosities table.
    
    Uses word-overlap matching against active curiosities to catch near-duplicates.
    Also checks the kanban tasks table for exact prefix matches (these items have
    already been processed into tasks and should not be re-injected).
    Returns True if a near-duplicate is found.
    """
    item_words = set(re.findall(r'\w{4,}', text.lower()))
    if len(item_words) < 3:
        return False
    
    # Check curiosities table for near-duplicates
    try:
        conn = sqlite3.connect(PROMETHEUS_DB_PATH, timeout=30)
        conn.execute("PRAGMA busy_timeout = 20000")
        rows = conn.execute(
            "SELECT text FROM curiosities WHERE status = 'active'"
        ).fetchall()
        conn.close()
        for (existing_text,) in rows:
            existing_words = set(re.findall(r'\w{4,}', (existing_text or '').lower()))
            if not existing_words:
                continue
            overlap = len(item_words & existing_words) / max(len(item_words | existing_words), 1)
            if overlap > 0.55:
                return True
    except Exception:
        pass  # If DB check fails, allow the injection
    
    # Check kanban tasks for exact prefix matches (already processed questions)
    kanban_path = os.path.expanduser("~/.hermes/kanban.db")
    if os.path.exists(kanban_path):
        try:
            kconn = sqlite3.connect(kanban_path, timeout=30)
            kconn.execute("PRAGMA busy_timeout = 20000")
            prefix = text[:80]
            match = kconn.execute(
                "SELECT COUNT(*) FROM tasks WHERE substr(title, 1, 80) = ?", (prefix,)
            ).fetchone()
            kconn.close()
            if match and match[0] > 0:
                return True
        except Exception:
            pass
    
    return False


def inject_diversity(queue, underrep_threads, existing_queue_texts):
    """Inject diversity questions from under-represented threads.
    
    Checks both the in-memory queue and the DB for duplicates.
    Returns items (dicts with text+source) ready for DB insertion.
    """
    injected = []
    for thread in underrep_threads[:3]:  # Max 3 injections
        if thread in DIVERSITY_INJECTIONS:
            for question in DIVERSITY_INJECTIONS[thread]:
                if is_already_in_queue(question, existing_queue_texts + [i["text"] for i in injected]):
                    continue
                if is_duplicate_in_db(question):
                    continue
                injected.append({"text": question, "source": "entropy_monitor"})
                break  # One per thread
    return injected


def main():
    parser = argparse.ArgumentParser(description="Queue Entropy Monitor")
    parser.add_argument("--inject", action="store_true", help="Inject diversity if below threshold")
    parser.add_argument("--force", action="store_true", help="Force inject regardless of threshold")
    args = parser.parse_args()

    state = load_state()
    queue = state.get("curiosity_queue", [])

    entropy, freq, threads = compute_queue_entropy(queue)
    total = len(queue)

    print(f"Queue Entropy: {entropy:.3f} bits ({total} items)")
    print(f"Threshold: {ENTROPY_THRESHOLD} bits | Target: {ENTROPY_TARGET} bits")

    if entropy < ENTROPY_THRESHOLD:
        print(f"\n  ⚠ STAGNATION RISK: entropy {entropy:.3f} < {ENTROPY_THRESHOLD}")
    elif entropy < ENTROPY_TARGET:
        print(f"\n  ⚠ BELOW TARGET: entropy {entropy:.3f} < {ENTROPY_TARGET}")
    else:
        print(f"\n  ✓ Healthy entropy")

    print(f"\nThread distribution:")
    for t, c in freq.most_common():
        bar = "#" * min(40, int(c / total * 40))
        pct = c / total * 100
        print(f"  {t:20s}: {c:3d} ({pct:4.1f}%) {bar}")

    effective_threads = sum(1 for c in freq.values() if c > 0)
    print(f"\nEffective threads: {effective_threads}")

    if args.inject or args.force:
        # Always inject when --inject is passed (not just when below threshold).
        # The diversity templates are limited (21 items) so this won't flood.
        if args.inject or entropy < ENTROPY_TARGET or args.force:
            underrep = get_underrepresented_threads(freq)
            if underrep:
                injected = inject_diversity(queue, underrep, queue)
                if injected:
                    # Write to prometheus.db as active curiosities.
                    # The sync pipeline (sync_curiosity_views.py) will pick
                    # them up on the next run (≤2 min latency). This follows
                    # the single-writer principle: sync_curiosity_views.py is
                    # the only writer to self_state.json.curiosity_queue.
                    conn = get_db(PROMETHEUS_DB_PATH)
                    now = time.time()
                    inserted = 0
                    for item in injected:
                        try:
                            conn.execute(
                                "INSERT INTO curiosities (text, status, source_experiment, created_at) "
                                "VALUES (?, 'active', 'entropy_monitor', ?)",
                                (item["text"], now)
                            )
                            inserted += 1
                        except Exception as e:
                            print(f"  DB insert failed for '{item['text'][:60]}': {e}")
                    conn.commit()
                    conn.close()
                    print(f"\nInjected {inserted} diversity items into prometheus.db:")
                    for item in injected:
                        thread = classify_thread(item["text"])
                        print(f"  [{thread}] {item['text'][:80]}")
                    print("  (will appear in queue on next sync run)")
                else:
                    print("\nAll candidates were duplicates — no injection needed")
            else:
                print("\nNo underrepresented threads found")
        else:
            print(f"\nEntropy above threshold — no injection needed")


if __name__ == "__main__":
    main()
