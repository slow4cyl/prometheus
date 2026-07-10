#!/usr/bin/env python3
"""
novelty_retrain.py — Retrain novelty predictor as curiosities resolve.

Pipeline:
  1. Label newly resolved curiosities (fast)
  2. Check if enough new labels to retrain
  3. Retrain model if threshold met
  4. Update curiosity_scorer_model.json

Protections: fcntl flock, db_retry, lock file.

Usage:
    python3 novelty_retrain.py           # Label new + retrain if needed
    python3 novelty_retrain.py --force   # Force retrain even if few new labels
    python3 novelty_retrain.py --stats   # Show label statistics
"""

import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB, under_home
import json
import os
import re
import sqlite3
import sys
import time

DB_PATH = _PP_PROMETHEUS_DB
LABELS_PATH = under_home("novelty_labels.json")
MODEL_PATH = under_home("curiosity_scorer_model.json")
LOCK_PATH = under_home(".novelty_retrain.lock")
STATE_PATH = under_home("novelty_retrain_state.json")

MIN_NEW_LABELS = 50  # Retrain after this many new labels


def get_db():
    from db_retry import get_db as _get_db
    return _get_db(DB_PATH)


def load_existing_labels():
    if os.path.exists(LABELS_PATH):
        with open(LABELS_PATH) as f:
            return json.load(f)
    return []


def save_labels(labels):
    with open(LABELS_PATH, 'w') as f:
        json.dump(labels, f)


def label_one(conn, cid, eid, text, created_at):
    """Label a single curiosity."""
    exp = conn.execute("SELECT domain, mechanism_type FROM experiments WHERE id = ?", (eid,)).fetchone()
    if not exp:
        return None
    domain, mechanism = exp

    m = re.search(r'\[TRANSFER from (\w+)\]', text or '')
    source_domain = m.group(1) if m else None

    # 1. New domain
    new_domain = 0
    if domain:
        r = conn.execute("SELECT COUNT(*) FROM experiments WHERE domain = ? AND created_at < ?",
                       (domain, created_at or 0)).fetchone()
        new_domain = 1 if r[0] == 0 else 0

    # 2. New edge (first few transfers to domain)
    new_edge = 0
    if domain:
        r = conn.execute("""
            SELECT COUNT(*) FROM experiments 
            WHERE domain = ? AND tags LIKE '%TRANSFER%' AND created_at < ?
        """, (domain, created_at or 0)).fetchone()
        new_edge = 1 if r[0] <= 2 else 0

    # 3. Claim survival
    claim_surv = 0
    if eid:
        r = conn.execute("""
            SELECT 1 FROM claim_evidence ce
            JOIN knowledge_claims kc ON kc.id = ce.claim_id
            WHERE ce.experiment_id = ? AND kc.status = 'ACTIVE' LIMIT 1
        """, (eid,)).fetchone()
        claim_surv = 1 if r else 0

    # 4. Mechanism novelty
    mech_novel = 0
    if mechanism:
        r = conn.execute("SELECT COUNT(*) FROM experiments WHERE mechanism_type = ?", (mechanism,)).fetchone()
        mech_novel = 1 if r[0] < 10 else 0

    # 5. Graph impact
    graph_impact = 0
    if domain:
        r = conn.execute("SELECT COUNT(*) FROM experiments WHERE domain = ? AND created_at < ?",
                       (domain, created_at or 0)).fetchone()
        graph_impact = 1 if r[0] < 5 else 0

    scores = {
        'new_domain': new_domain, 'new_edge': new_edge,
        'claim_survival': claim_surv, 'mechanism_novelty': mech_novel,
        'graph_impact': graph_impact,
    }
    weights = {'new_domain': 0.25, 'new_edge': 0.20, 'claim_survival': 0.30,
               'mechanism_novelty': 0.15, 'graph_impact': 0.10}
    novelty_score = sum(scores[k] * weights[k] for k in scores)
    is_novel = 1 if novelty_score >= 0.3 else 0

    return {
        'curiosity_id': cid, 'experiment_id': eid,
        'text': (text or '')[:200], 'scores': scores,
        'novelty_score': round(novelty_score, 4), 'is_novel': is_novel,
    }


def text_features(text):
    if not text: return {}
    tl = text.lower()
    return {
        'text_length': min(len(text) / 500, 1.0),
        'has_source_domain': 1 if '[TRANSFER from' in text else 0,
        'has_target_domain': 1 if 'apply to' in tl or 'predict' in tl else 0,
        'has_transfer_tag': 1 if '[TRANSFER]' in text else 0,
        'question_words': sum(1 for w in ['does','can','is','will','how','why','what'] if w in tl.split()),
        'has_number': 1 if any(c.isdigit() for c in text) else 0,
        'causal_count': sum(1 for w in ['cause','effect','mechanism','why','because'] if w in tl),
        'boundary_count': sum(1 for w in ['boundary','threshold','limit'] if w in tl),
        'contrast_words': sum(1 for w in ['versus','compared','instead'] if w in tl),
        'transfer_words': sum(1 for w in ['transfer','apply','generalize'] if w in tl),
        'novel_words': sum(1 for w in ['novel','new','first','surprising'] if w in tl),
        'paradox_words': sum(1 for w in ['paradox','counterintuitive','unexpected'] if w in tl),
    }


def retrain(labels):
    """Retrain model on all labels."""
    import numpy as np

    X, y = [], []
    for l in labels:
        tf = text_features(l['text'])
        X.append(list(tf.values()))
        y.append(l['is_novel'])

    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=float)
    feat_names = list(text_features(labels[0]['text']).keys())

    X_norm = np.column_stack([np.ones(len(X_arr)), X_arr])
    all_names = ['bias'] + feat_names

    weights = np.zeros(len(all_names))
    for _ in range(1000):
        logits = X_norm @ weights
        probs = 1 / (1 + np.exp(-np.clip(logits, -500, 500)))
        grad = X_norm.T @ (probs - y_arr) / len(y_arr)
        weights -= 0.01 * grad

    probs = 1 / (1 + np.exp(-np.clip(X_norm @ weights, -500, 500)))
    preds = (probs >= 0.5).astype(int)
    acc = float((preds == y_arr).mean())

    novel_p = probs[y_arr == 1]
    non_novel_p = probs[y_arr == 0]
    disc = float(novel_p.mean() / max(non_novel_p.mean(), 0.001))

    weights_dict = {all_names[i]: float(weights[i]) for i in range(len(all_names))}

    return {
        'novel': {
            'weights': weights_dict, 'bias': float(weights[0]),
            'feature_names': feat_names, 'is_combined': False,
            'uses_novelty_scores': False,
        },
        'accuracy': acc, 'discrimination': disc,
        'n_positive': int(y_arr.sum()), 'n_total': len(y_arr),
    }


def main():
    parser = argparse.ArgumentParser(description="Novelty retrain pipeline")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    # Flock
    import fcntl
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("Another instance running, skipping.")
        lock_fd.close()
        return

    try:
        conn = get_db()

        # Load existing labels
        existing = load_existing_labels()
        existing_ids = {l['curiosity_id'] for l in existing}

        # Find newly resolved curiosities
        new_rows = conn.execute("""
            SELECT c.id, c.resolved_by_experiment, c.text, c.created_at
            FROM curiosities c
            WHERE c.resolved_by_experiment IS NOT NULL
            AND c.text IS NOT NULL AND c.text != ''
            AND c.id NOT IN (SELECT curiosity_id FROM (SELECT '' as curiosity_id))
        """).fetchall()

        # Filter to only new ones
        new_labels = []
        for cid, eid, text, created_at in new_rows:
            if cid not in existing_ids:
                label = label_one(conn, cid, eid, text, created_at)
                if label:
                    new_labels.append(label)

        # Also re-label any existing labels that might have changed status
        # (claims can become ACTIVE after labeling)
        updated = 0
        for l in existing:
            if l['experiment_id']:
                r = conn.execute("""
                    SELECT 1 FROM claim_evidence ce
                    JOIN knowledge_claims kc ON kc.id = ce.claim_id
                    WHERE ce.experiment_id = ? AND kc.status = 'ACTIVE' LIMIT 1
                """, (l['experiment_id'],)).fetchone()
                new_surv = 1 if r else 0
                if l['scores'].get('claim_survival', 0) != new_surv:
                    l['scores']['claim_survival'] = new_surv
                    weights = {'new_domain': 0.25, 'new_edge': 0.20, 'claim_survival': 0.30,
                               'mechanism_novelty': 0.15, 'graph_impact': 0.10}
                    l['novelty_score'] = round(sum(l['scores'][k] * weights[k] for k in l['scores']), 4)
                    l['is_novel'] = 1 if l['novelty_score'] >= 0.3 else 0
                    updated += 1

        # Merge
        all_labels = existing + new_labels
        save_labels(all_labels)

        print(f"Labels: {len(existing)} existing + {len(new_labels)} new = {len(all_labels)} total ({updated} updated)")

        # Check if we should retrain
        should_retrain = args.force or len(new_labels) >= MIN_NEW_LABELS
        
        if should_retrain and len(all_labels) >= 100:
            print("Retraining model...")
            result = retrain(all_labels)

            # Load and update model
            with open(MODEL_PATH) as f:
                model = json.load(f)
            model.setdefault('metadata', {})
            model['models']['novel'] = result['novel']
            model['metadata']['novel_accuracy'] = result['accuracy']
            model['metadata']['novel_trained_at'] = time.time()
            model['metadata']['novel_n_positive'] = result['n_positive']
            model['metadata']['novel_n_total'] = result['n_total']
            model['metadata']['novel_discrimination'] = f"{result['discrimination']:.1f}x"

            with open(MODEL_PATH, 'w') as f:
                json.dump(model, f, indent=2)

            print(f"  Accuracy: {result['accuracy']:.3f}")
            print(f"  Discrimination: {result['discrimination']:.1f}x")
            print(f"  Novel examples: {result['n_positive']}/{result['n_total']}")
        else:
            print(f"  Need {MIN_NEW_LABELS} new labels to retrain (have {len(new_labels)})")

        conn.close()

        # Save state
        with open(STATE_PATH, 'w') as f:
            json.dump({
                'last_run': time.time(),
                'total_labels': len(all_labels),
                'new_labels': len(new_labels),
                'updated': updated,
                'retrained': should_retrain and len(all_labels) >= 100,
            }, f)

    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError: pass
        lock_fd.close()


if __name__ == "__main__":
    main()
