#!/usr/bin/env python3
"""
Multi-Objective Curiosity Scorer
=================================
Replaces single scalar score with three objective predictions:
  P(confirm)  — probability the experiment will confirm
  P(novel)    — probability the experiment will discover something novel
  P(expand)   — probability the experiment will reach a new domain

Each curiosity gets a vector [P(confirm), P(novel), P(expand)] instead
of a single 0-100 score. The scheduler allocates budget across objectives.

Backward compatible: also outputs a combined score for existing consumers.

Usage:
    python3 curiosity_multi_scorer.py "Does X transfer from A to B?"
    python3 curiosity_multi_scorer.py --batch-input curiosities.json
    python3 curiosity_multi_scorer.py --calibrate  # retrain on latest data
"""

import re
import json
import os
import sys
import sqlite3
import math
import time
from collections import Counter

DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")
MODEL_PATH = os.path.expanduser("~/.hermes/curiosity_scorer_model.json")

# Feature extraction (same as validator + curiosity_scorer)
MECHANISM_KW = [
    'mechanism', 'because', 'due to', 'caused by', 'results from',
    'driven by', 'explain', 'transfer', 'apply', 'generalize'
]

BOUNDARY_KW = [
    'when', 'under what', 'conditions', 'boundary', 'limit', 'fail',
    'break', 'except', 'only if', 'depends', 'asymmetric'
]

TRANSFER_KW = [
    'transfer', 'cross-domain', 'cross domain', 'generaliz', 'apply to',
    'does.*transfer', 'can.*transfer'
]

NOVELTY_KW = [
    'novel', 'first time', 'new finding', 'surprising', 'contrary',
    'paradigm', 'fundamental', 'universal', 'breakthrough'
]

EXPANSION_KW = [
    'new domain', 'unexplored', 'never tested', 'first application',
    'extends to', 'opens up', 'unexplored territory'
]


def extract_features(text):
    """Extract features from curiosity text."""
    if not text:
        return {}
    t = text.lower()
    features = {}

    # Structural features
    features['has_transfer_tag'] = 1 if '[transfer]' in t else 0
    features['transfer_words'] = sum(1 for w in TRANSFER_KW if re.search(w, t))
    features['has_source_domain'] = 1 if re.search(r'from\s+\w+', t) else 0
    features['has_target_domain'] = 1 if re.search(r'(?:to|in|for|across)\s+\w+', t) else 0
    features['has_number'] = 1 if re.search(r'\d+', t) else 0
    features['word_count'] = len(t.split())

    # Content features
    features['mechanism_count'] = sum(1 for w in MECHANISM_KW if w in t)
    features['boundary_count'] = sum(1 for w in BOUNDARY_KW if w in t)
    features['causal_count'] = sum(1 for w in ['cause', 'effect', 'influence', 'impact', 'leads to', 'predict'] if w in t)
    features['specificity'] = sum(1 for w in ['specifically', 'quantify', 'measure', 'threshold', 'minimum', 'maximum'] if w in t)

    # Novelty signals
    features['novelty_words'] = sum(1 for w in NOVELTY_KW if w in t)
    features['question_words'] = sum(1 for w in ['what', 'how', 'does', 'can', 'is'] if w in t.split())

    # Expansion signals
    features['expansion_words'] = sum(1 for w in EXPANSION_KW if w in t)

    return features


FEAT_NAMES = [
    'has_transfer_tag', 'transfer_words', 'has_source_domain', 'has_target_domain',
    'has_number', 'word_count', 'mechanism_count', 'boundary_count', 'causal_count',
    'specificity', 'novelty_words', 'question_words', 'expansion_words'
]


def sigmoid(x):
    # Numerically stable logistic sigmoid, output strictly in (0, 1).
    # NOTE (2026-06-14): the previous implementation was
    #     return 1 / (1 + max(-500, min(500, -x)))
    # which omitted math.exp entirely — it computed 1/(1 + clamp(-x)), an
    # UNBOUNDED function that returned values outside [0,1] (and negative ones)
    # whenever |x| was non-trivial. It stayed hidden only because the trained
    # models had near-zero weights (z ~= bias ~= small). Any model with real
    # signal (e.g. the repaired 'expand' objective) drove z into a range where
    # the broken formula produced p_expand=7.5, p_novel=-0.002, etc., which in
    # turn corrupted combined_score. This is the genuine logistic sigmoid.
    if x >= 0:
        z = math.exp(-min(500, x))
        return 1.0 / (1.0 + z)
    z = math.exp(min(500, x))
    return z / (1.0 + z)


def normalize_features(features, feat_min=None, feat_max=None):
    """Normalize features to 0-1 range."""
    if feat_min is None:
        feat_min = {f: 0 for f in FEAT_NAMES}
    if feat_max is None:
        feat_max = {f: 1 for f in FEAT_NAMES}
    return {
        f: (features.get(f, 0) - feat_min[f]) / max(feat_max[f] - feat_min[f], 1)
        for f in FEAT_NAMES
    }


def load_model():
    """Load trained model weights."""
    if os.path.exists(MODEL_PATH):
        with open(MODEL_PATH) as f:
            return json.load(f)
    return None


def save_model(model):
    """Save trained model weights."""
    with open(MODEL_PATH, 'w') as f:
        json.dump(model, f, indent=2)


def train_model(data, objective, lr=0.01, epochs=300, l2=0.005):
    """Train logistic regression for a single objective."""
    # Compute feature ranges
    feat_min = {f: min(d['features'][f] for d in data) for f in FEAT_NAMES}
    feat_max = {f: max(d['features'][f] for d in data) for f in FEAT_NAMES}

    weights = {f: 0.0 for f in FEAT_NAMES}
    bias = 0.0

    for epoch in range(epochs):
        total_loss = 0
        for d in data:
            nf = normalize_features(d['features'], feat_min, feat_max)
            z = sum(weights[f] * nf[f] for f in FEAT_NAMES) + bias
            pred = sigmoid(z)
            label = d[objective]
            error = pred - label
            total_loss += error ** 2

            for f in FEAT_NAMES:
                weights[f] -= lr * (error * nf[f] + l2 * weights[f])
            bias -= lr * error

    return {
        'weights': weights,
        'bias': bias,
        'feat_min': feat_min,
        'feat_max': feat_max,
        'objective': objective,
        'train_loss': total_loss / len(data),
    }


def predict(features, model):
    """Predict using trained model."""
    feat_min = model.get('feat_min', {f: 0 for f in FEAT_NAMES})
    feat_max = model.get('feat_max', {f: 1 for f in FEAT_NAMES})
    nf = normalize_features(features, feat_min, feat_max)
    z = sum(model['weights'].get(f, 0) * nf[f] for f in FEAT_NAMES if f in model['weights']) + model['bias']
    return sigmoid(z)


def calibrate():
    """Retrain all three models on latest data."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    c = conn.cursor()

    # Build labeled dataset
    c.execute('''
        SELECT c.id, c.text, c.score, e.result, e.domain, e.tags
        FROM curiosities c
        JOIN experiments e ON c.resolved_by_experiment = e.id
        WHERE c.resolved_by_experiment IS NOT NULL
        AND e.result IS NOT NULL
    ''')
    rows = c.fetchall()

    data = []
    for cid, text, score, result, domain, tags in rows:
        if isinstance(text, dict):
            text = text.get('text', str(text))
        if not text or len(text) < 10:
            continue

        # Labels
        is_confirmed = 1 if ('CONFIRMED' in result or 'SUPPORTED' in result) else 0
        is_refuted = 1 if 'REFUTED' in result else 0
        if not is_confirmed and not is_refuted:
            continue

        features = extract_features(text)

        # P(novel): use novelty words + boundary conditions + multi-mechanism
        is_novel = 1 if (features['novelty_words'] > 0 or
                         features['boundary_count'] > 0 or
                         features['mechanism_count'] >= 2) else 0

        # P(expand): does this experiment reach toward another domain?
        # FIXED 2026-06-14: the old definition (target domain has <10 experiments)
        # was structurally self-defeating — by the time an experiment RESOLVES,
        # its domain almost always already has >=10 experiments, so the label
        # was positive only 0.04% of the time (8/20806). The model correctly
        # learned "always ~0", which killed the expand objective and made the
        # expand task-selection lane effectively random.
        # New definition: cross-domain reach, detectable at resolution time via
        #   (a) the experiment's TRANSFER tag (fires ~63% — a learnable signal), OR
        #   (b) transfer/expansion language in the curiosity text, OR
        #   (c) the original sparse-domain heuristic (kept as a weak secondary).
        is_expansion = 0
        _tags_l = (tags or "").lower()
        if 'transfer' in _tags_l:
            is_expansion = 1
        elif features.get('has_transfer_tag') or features.get('transfer_words', 0) > 0 \
                or features.get('expansion_words', 0) > 0:
            is_expansion = 1
        elif domain:
            try:
                c2 = conn.cursor()
                c2.execute('SELECT COUNT(*) FROM experiments WHERE domain = ?', (domain,))
                count = c2.fetchone()[0]
                is_expansion = 1 if count < 10 else 0
            except Exception:
                pass

        # P(break): is this a BOUNDARY-SEEKING QUESTION — one that hunts for where
        # a mechanism stops holding? Added 2026-06-15 to close the portfolio-allocator
        # leak: the allocator assigns ~28% weight to `break`, but no per-curiosity
        # p_break existed, so batch_create_tasks.py could not SELECT break-oriented
        # items and the weight evaporated at selection time.
        #
        # LABEL CHOICE (important): we label the QUESTION's intent, NOT the experiment
        # outcome. A first attempt labeled "the resolution was REFUTED" — but that's
        # ~49% positive and nearly the inverse of `confirm`, so it's unlearnable from
        # text (trained to coin-flip accuracy 0.49). What the `break` lane should
        # actually route toward is questions that GO LOOKING for a boundary
        # ("under what conditions does X fail / where does Y break"), which is a
        # property of the curiosity text and is cleanly learnable (~9% positive,
        # boundary-keyword driven). This matches the architecture-map framing:
        # the system's job is to LOCATE boundary conditions, so prioritize the
        # questions aimed at them.
        _text_l = text.lower()
        is_break = 1 if (
            features.get('boundary_count', 0) >= 2
            or 'under what condition' in _text_l
            or 'where does' in _text_l
            or 'at what' in _text_l
            or 'boundary' in _text_l
            or 'fails when' in _text_l
            or 'breaks' in _text_l
            or 'break ' in _text_l
        ) else 0

        data.append({
            'features': features,
            'confirm': is_confirmed,
            'novel': is_novel,
            'expand': is_expansion,
            'break': is_break,
        })

    conn.close()

    print(f"Training on {len(data)} labeled examples")

    # Train four models
    models = {}
    for objective in ['confirm', 'novel', 'expand', 'break']:
        model = train_model(data, objective)
        models[objective] = model

        # Compute accuracy on training data
        correct = sum(1 for d in data if (predict(d['features'], model) >= 0.5) == d[objective])
        accuracy = correct / len(data)
        print(f"  {objective}: train_loss={model['train_loss']:.4f}, accuracy={accuracy:.3f}")

    # Save combined model
    combined = {
        'version': 1,
        'trained_at': time.time(),
        'n_examples': len(data),
        'feat_names': FEAT_NAMES,
        'models': models,
    }
    save_model(combined)
    print(f"\nModel saved to {MODEL_PATH}")

    return combined


try:
    from fertility_predictor_helper import predict_fertility
except ImportError:
    predict_fertility = lambda *a, **k: 0.0


def score_curiosity(text, model=None, evidence_depth=0, surprise=0.0,
                    transfer_confirm_discount=1.0, shape_confirm_discount=1.0):
    """Score a single curiosity across all objectives.

    surprise (0..1): how often the fleet's preregistered priors are WRONG in this
    curiosity's domain (from prior_override_report's by-domain confirmation rate;
    0 = at/above the global confirmation baseline or too few samples to trust).
    A surprising domain is where an experiment buys the most information, so it
    adds a bounded bonus that lifts EXPLORATION-leaning questions (novel/expand/
    break) there. Purely additive and capped — it never lowers a score, so it
    re-ranks toward high-information domains without starving anything. Default
    0.0 keeps every existing caller's behavior byte-identical.

    transfer_confirm_discount (0.75..1.0): bounded multiplier on p_confirm for
    [TRANSFER]-shaped questions, sourced from the meta-claim prober's measured
    record of how often the system's own "this mechanism generalizes" beliefs
    hold in an unseen domain (meta_transfer_calibration.json — only ~1 in 5
    self-asserted transfers held cleanly on first measurement). Keeps inflated
    self-belief from crowding the confirm lane; floor 0.75 so it re-ranks, not
    starves. Default 1.0 keeps every existing caller byte-identical.

    shape_confirm_discount (0.85..1.0): bounded multiplier on p_confirm from
    the mechanism-SHAPE calibration (mechanism_calibration.json — e.g.
    MONOTONIC claims' preregistered priors confirm 11.5pp below the overall
    rate, the most over-trusted shape). The caller classifies the question's
    shape and passes that shape's discount; floor 0.85. Composes
    multiplicatively with the transfer discount under an overall floor of
    0.70. Default 1.0 = no-op."""
    if model is None:
        model = load_model()
    if model is None:
        # No trained model — use heuristic defaults
        features = extract_features(text)
        return {
            'p_confirm': 0.65,  # baseline
            'p_novel': 0.3,
            'p_expand': 0.1,
            'p_break': 0.2,
            'features': features,
            'combined': 50,
            'model': 'heuristic',
        }

    features = extract_features(text)
    scores = {}
    for objective in ['confirm', 'novel', 'expand']:
        scores[f'p_{objective}'] = round(predict(features, model['models'][objective]), 4)
    # p_break is optional: models trained before 2026-06-15 won't have it.
    # Fall back to a boundary-language heuristic so the column is never NULL
    # and the break selection lane always has something to rank.
    if 'break' in model.get('models', {}):
        scores['p_break'] = round(predict(features, model['models']['break']), 4)
    else:
        _bc = features.get('boundary_count', 0)
        scores['p_break'] = round(min(0.9, 0.15 + 0.2 * _bc), 4)

    # Calibration haircuts (see docstring): applied to the stored p_confirm
    # BEFORE the combined score so both the confirm-lane ranking and the
    # combined ranking see the discounted value. The two discounts measure
    # DIFFERENT failure modes (self-asserted transfer generalization vs
    # mechanism-shape over-trust) and compose multiplicatively, clamped at an
    # overall floor so a question hit by both re-ranks rather than starves.
    _disc = 1.0
    if transfer_confirm_discount < 1.0 and '[TRANSFER' in text.upper():
        _disc *= max(transfer_confirm_discount, 0.75)
    if shape_confirm_discount < 1.0:
        _disc *= max(shape_confirm_discount, 0.85)
    if _disc < 1.0:
        scores['p_confirm'] = round(scores['p_confirm'] * max(_disc, 0.70), 4)

    # Combined score: dynamic weights from portfolio allocator.
    # Falls back to 0.5/0.3/0.2 if no allocator state exists.
    #
    # IMPORTANT: the portfolio may define MORE dimensions than this 3-term
    # formula consumes (e.g. it added a 'break' dimension on 2026-06-13).
    # If we used the raw weights, the mass assigned to dimensions we don't
    # multiply in here would be silently dropped, capping the score (the
    # observed combined_score~27 ceiling: only 72.7% of weight mass applied).
    # We therefore RENORMALIZE the weights of the three objectives we actually
    # score so they sum to 1.0 — making the ceiling independent of how many
    # other dimensions the portfolio tracks.
    _weights = {"confirm": 0.5, "novel": 0.3, "expand": 0.2}
    try:
        import os as _os
        _ps_path = _os.path.expanduser("~/.hermes/portfolio_state.json")
        if _os.path.exists(_ps_path):
            import json as _json
            with open(_ps_path) as _f:
                _ps = _json.load(_f)
                _weights = _ps.get("current", _weights)
    except Exception:
        pass

    # Renormalize across only the objectives this formula multiplies in.
    _w_confirm = _weights.get("confirm", 0.5)
    _w_novel = _weights.get("novel", 0.3)
    _w_expand = _weights.get("expand", 0.2)
    _w_sum = _w_confirm + _w_novel + _w_expand
    if _w_sum > 0:
        _w_confirm, _w_novel, _w_expand = (
            _w_confirm / _w_sum, _w_novel / _w_sum, _w_expand / _w_sum,
        )

    combined = int((
        _w_confirm * scores['p_confirm'] +
        _w_novel * scores['p_novel'] +
        _w_expand * scores['p_expand']
    ) * 100)

    # Evidence depth bonus (Hypothesis P-001): questions grounded in experimental
    # evidence are more fertile. Boost by depth*5, capped at 25.
    evidence_depth_bonus = min(evidence_depth * 3, 15)

    fertility_bonus = int(predict_fertility(evidence_depth, True, 'worker', 'transfer') * 15)

    # Surprise bonus: in domains where priors are often wrong, lift the
    # exploration-leaning questions (novel/expand/break — this is also the only
    # place p_break enters the combined score). Scaled by the mean exploration
    # probability so a confirm-heavy question in a surprising domain gets little,
    # a break/novel question gets the most. Bounded so it re-ranks, not dominates.
    explore = (scores['p_novel'] + scores['p_expand'] + scores['p_break']) / 3.0
    surprise_bonus = int(round(min(max(surprise, 0.0), 1.0) * explore * 25))
    surprise_bonus = min(surprise_bonus, 12)

    combined = min(100, max(0, combined + evidence_depth_bonus + fertility_bonus
                            + surprise_bonus))

    return {
        **scores,
        'features': features,
        'combined': combined,
        'evidence_depth_bonus': evidence_depth_bonus,
        'fertility_bonus': fertility_bonus,
        'surprise_bonus': surprise_bonus,
        'model': 'trained',
    }


def score_batch(texts, model=None):
    """Score a batch of curiosities."""
    if model is None:
        model = load_model()
    return [score_curiosity(t, model) for t in texts]


def format_report(texts, scores):
    """Format scoring results as readable report."""
    lines = []
    lines.append("=" * 70)
    lines.append("MULTI-OBJECTIVE CURIOSITY SCORES")
    lines.append("=" * 70)

    for i, (text, s) in enumerate(zip(texts, scores)):
        lines.append(f"\n--- Curiosity {i+1} ---")
        lines.append(f"Text: {text[:120]}...")
        lines.append(f"P(confirm): {s['p_confirm']:.3f}  "
                     f"P(novel): {s['p_novel']:.3f}  "
                     f"P(expand): {s['p_expand']:.3f}  "
                     f"Combined: {s['combined']}")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Multi-Objective Curiosity Scorer")
    parser.add_argument('text', nargs='?', help="Curiosity text to score")
    parser.add_argument('--calibrate', action='store_true', help="Retrain on latest data")
    parser.add_argument('--batch-input', help="JSON file with list of curiosities")
    parser.add_argument('--json', action='store_true', help="Output as JSON")
    args = parser.parse_args()

    if args.calibrate:
        calibrate()
        return

    if args.batch_input:
        with open(args.batch_input) as f:
            texts = json.load(f)
    elif args.text:
        texts = [args.text]
    else:
        print("Provide text, --batch-input, or --calibrate")
        return

    scores = score_batch(texts)

    if args.json:
        print(json.dumps(scores, indent=2))
    else:
        print(format_report(texts, scores))


if __name__ == '__main__':
    main()
