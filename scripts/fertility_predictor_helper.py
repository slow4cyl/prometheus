#!/usr/bin/env python3
"""Fertility prediction helper for curiosity scoring."""
import json
import os
import math

_FERTILITY_MODEL = None
_FERTILITY_MODEL_PATH = os.path.expanduser("~/.hermes/models/fertility_predictor.json")

def load_fertility_model():
    global _FERTILITY_MODEL
    if _FERTILITY_MODEL is not None:
        return _FERTILITY_MODEL
    try:
        with open(_FERTILITY_MODEL_PATH) as f:
            _FERTILITY_MODEL = json.load(f)
    except Exception:
        _FERTILITY_MODEL = {}
    return _FERTILITY_MODEL

def predict_fertility(evidence_depth, has_parent, source_type, question_type):
    """Predict probability a question will be fertile. Returns 0-1."""
    fm = load_fertility_model()
    if not fm or 'coef' not in fm:
        return 0.0
    
    features = [
        evidence_depth,
        1 if has_parent else 0,
        1 if source_type == 'worker' else 0,
        1 if source_type == 'synthesis' else 0,
        1 if source_type == 'compression' else 0,
        1 if question_type == 'transfer' else 0,
        1 if question_type == 'boundary' else 0,
        1 if question_type == 'mechanism' else 0,
    ]
    
    means = fm.get('scaler_mean', [0]*8)
    scales = fm.get('scaler_scale', [1]*8)
    scaled = [(features[i] - means[i]) / scales[i] if scales[i] != 0 else 0 for i in range(8)]
    
    coef = fm.get('coef', [0]*8)
    intercept = fm.get('intercept', 0)
    z = sum(scaled[i] * coef[i] for i in range(8)) + intercept
    
    return 1.0 / (1.0 + math.exp(-max(-500, min(500, z))))
