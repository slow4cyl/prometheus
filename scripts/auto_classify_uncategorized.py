#!/usr/bin/env python3
"""
Auto-classify uncategorized experiments using keyword matching against
the existing domain taxonomy. Runs on experiments with domain='' or
domain='uncategorized' or domain IS NULL.

Used in two modes:
  1. Backfill: classify existing uncategorized experiments
  2. Pipeline: called after experiment creation to assign domain

The keyword sets are derived from the CANONICAL_MAPPINGS in
domain_taxonomy_merge.py — the same domain vocabulary the system
already uses.

Usage:
    python3 auto_classify_uncategorized.py           # Backfill all
    python3 auto_classify_uncategorized.py --dry-run # Show what would change
    python3 auto_classify_uncategorized.py --id exp_123  # Classify one
"""

import argparse
import os
import re
import sqlite3
import sys
import time
from db_retry import get_db

"""CLI tool: Auto Classify Uncategorized.

Usage: python3 auto_classify_uncategorized.py [options]
"""


DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")

# ─── Domain keyword sets ────────────────────────────────────────────────────
# Derived from CANONICAL_MAPPINGS. Each domain maps to a list of keywords
# that appear in hypothesis/result text for that domain.
# Order matters: first match wins. More specific domains before general.

DOMAIN_KEYWORDS = {
    "security": [
        "injection", "prompt injection", "jailbreak", "adversarial attack",
        "adversarial robustness", "security", "attack", "defense", "defence",
        "toxicity", "content moderation", "safety training", "red team",
        "penetration", "vulnerability", "exploit", "malware", "phishing",
        "social engineering", "authentication", "authorization",
        "network intrusion", "intrusion detection", "spam detection",
        "steganography", "watermarking", "backdoor", "trojan",
    ],
    "injection_detection": [
        "injection detection", "prompt injection detection", "injected",
        "injection classifier", "injection detector", "code injection",
        "dialogue injection", "CJK injection", "pre-injection",
    ],
    "machine_learning": [
        "machine learning", "deep learning", "neural network", "transformer",
        "fine-tuning", "fine tuning", "training", "loss function",
        "gradient", "backpropagation", "overfitting", "regularization",
        "hyperparameter", "model architecture", "attention mechanism",
        "embedding", "representation learning", "transfer learning",
        "few-shot", "zero-shot", "data augmentation", "quantization",
        "pruning", "distillation", "knowledge distillation", "scaling law",
        "inference optimization", "edge deployment", "FPGA", "GPU",
        "SIMD", "INT8", "INT4", "INT2", "latency", "throughput",
        "inference", "MLOps", "production ML", "model compression",
    ],
    "nlp": [
        "natural language", "NLP", "text classification", "sentiment",
        "named entity", "NER", "parsing", "tokenization", "language model",
        "text generation", "summarization", "translation", "question answering",
        "text style", "style transfer", "authorship", "stylometry",
        "attribution", "text detection", "text classification",
    ],
    "ai_safety": [
        "AI safety", "alignment", "RLHF", "human feedback", "reward model",
        "value alignment", "corrigibility", "containment", "AI alignment",
        "authority deference", "sycophancy", "honesty", "truthfulness",
        "calibrated", "uncertainty", "refusal", "overconfidence",
        "selective prediction", "selective routing", "safety classifier",
    ],
    "calibration": [
        "calibration", "confidence calibration", "temperature scaling",
        "Platt scaling", "isotonic", "reliability diagram", "ECE",
        "expected calibration error", "Brier score", "confidence interval",
        "selective prediction", "abstention", "know what you don't know",
    ],
    "cross_domain_transfer": [
        "cross-domain", "cross domain", "transfer learning", "domain adaptation",
        "domain shift", "generalization", "multi-domain", "domain gap",
        "cross-system", "transfer", "generalize",
    ],
    "ensemble_methods": [
        "ensemble", "committee", "voting", "bagging", "boosting",
        "random forest", "model averaging", "mixture of experts",
        "subadditivity", "disagreement", "contradiction probing",
    ],
    "adversarial_ml": [
        "adversarial ML", "adversarial machine learning", "FGSM", "PGD",
        "adversarial example", "adversarial perturbation", "robustness",
        "adversarial training", "adversarial defense", "adversarial attack",
        "gradient attack", "evasion attack", "poisoning attack",
    ],
    "infrastructure_expanded": [
        "SciPy", "scipy", "bottleneck", "sklearn", "sparse",
        "streaming", "adaptive", "pipeline does not",
        "scipy_bottleneck", "streaming_adaptive", "test_hermes_home",
        "HERMES_HOME",
    ],
    "meta_analysis": [
        "meta-analysis", "meta analysis", "meta experiment", "meta research",
        "experiment fatigue", "research efficiency", "hypothesis generation",
        "confirmation bias", "diminishing returns", "local optimum",
        "research trajectory", "experiment scheduling", "cold start",
        "causal graph", "information density", "pareto frontier",
        "batch size", "optimal number", "cost per discovery",
        "self-application", "meta-skill", "reusable meta",
        "variance pre-screen", "skip N-shot", "low-variance domains",
        "LEGITIMATELY LOW", "Structured findings template",
        "findings template",
    ],
    "agent_systems": [
        "agent", "multi-agent", "AGI", "autonomous", "self-improvement",
        "self-modification", "cognitive", "reasoning", "planning",
        "tool use", "tool calling", "function calling", "agentic",
    ],
    "audio_security": [
        "audio", "speech", "voice", "speaker", "acoustic", "prosody",
        "audio adversarial", "deepfake audio", "voice clone",
        "audio injection", "audio attack", "sound", "frequency",
        "spectral", "MFCC", "mel spectrogram",
    ],
    "financial_fraud_detection": [
        "fraud", "financial", "credit card", "transaction", "anomaly detection",
        "outlier detection", "Isolation Forest", "autoencoder fraud",
        "anomalous", "suspicious", "money laundering", "AML",
    ],
    "hallucination_detection": [
        "hallucination", "factual error", "confabulation", "faithfulness",
        "groundedness", "fact check", "fact verification", "hallucinate",
    ],
    "rag": [
        "RAG", "retrieval augmented", "retrieval-augmented", "retrieval",
        "vector database", "semantic search", "chunk", "embedding search",
        "context window", "relevant documents", "knowledge base",
    ],
    "vision": [
        "image", "visual", "computer vision", "object detection",
        "segmentation", "VQA", "visual question", "multimodal",
        "image classification", "convolutional", "CNN", "vision transformer",
        "ViT", "CLIP", "image adversarial",
    ],
    "network_security": [
        "network", "packet", "firewall", "IDS", "IPS", "network intrusion",
        "network traffic", "network anomaly", "botnet", "DDoS",
        "obfuscation", "network detection",
    ],
    "medical_nlp": [
        "medical", "clinical", "patient", "diagnosis", "EHR",
        "electronic health", "radiology", "pathology", "drug",
        "pharmaceutical", "biomedical", "healthcare", "hospital",
    ],
    "interpretability": [
        "interpretability", "explainability", "attention visualization",
        "probing", "mechanistic", "feature attribution", "SHAP",
        "LIME", "saliency", "concept", "neuron", "circuit",
    ],
    "code_analysis": [
        "code", "software", "programming", "vulnerability", "static analysis",
        "binary", "reverse engineering", "decompilation", "AST",
    ],
    "style_attribution": [
        "style", "authorship", "stylometry", "attribution", "writing style",
        "text style", "linguistic fingerprint", "writeprint",
    ],
    "data_quality": [
        "data quality", "label noise", "data cleaning", "data validation",
        "annotation", "labeling", "ground truth", "dataset",
    ],
    "tfidf": [
        "TF-IDF", "tfidf", "term frequency", "inverse document frequency",
        "feature importance", "signal concentration", "logistic regression",
    ],
    "defense": [
        "defense mechanism", "defence", "protect", "guard", "shield",
        "robust", "resilient", "hardened",
    ],
    "social_engineering": [
        "social engineering", "phishing", "pretexting", "baiting",
        "tailgating", "deception", "manipulation",
    ],
    "regulatory": [
        "regulation", "compliance", "GDPR", "EU AI Act", "FDA",
        "Basel III", "COPPA", "legal", "policy", "governance",
    ],
    "dedup_pipeline": [
        "dedup", "deduplication", "duplicate detection", "near-duplicate",
        "similarity", "Jaccard", "cosine similarity",
    ],
    "model_monitoring": [
        "monitoring", "drift detection", "data drift", "concept drift",
        "model health", "performance degradation", "alert",
    ],
    "distillation": [
        "distillation", "teacher student", "knowledge distillation",
        "self-distillation", "feature distillation",
    ],
    "rlhf": [
        "RLHF", "reinforcement learning from human feedback",
        "reward model", "PPO", "DPO", "GRPO",
    ],
    "cross_lingual": [
        "cross-lingual", "multilingual", "code-mixed", "code-switching",
        "language", "translation", "multilingual NLP",
    ],
    "embedding": [
        "embedding", "vector representation", "word2vec", "word embedding",
        "sentence embedding", "semantic embedding",
    ],
    "safety": [
        "safety", "harm", "hazard", "risk", "danger", "toxic",
        "hate speech", "bias", "fairness", "discrimination",
    ],
    "hardware": [
        "hardware", "edge device", "Raspberry Pi", "FPGA", "ARM",
        "Neon", "Jetson", "quantization hardware", "SIMD",
    ],
    "attack": [
        "attack", "attacker", "adversary", "threat", "offensive",
    ],
    "llm_training": [
        "LLM training", "pre-training", "pretraining", "instruction tuning",
        "SFT", "supervised fine-tuning", "training data",
    ],
    "model_monitoring": [
        "monitoring", "drift", "degradation", "performance tracking",
    ],
    "graduated_scope": [
        "DICT", "DISPATCH", "PLATFORM", "fact reduction", "fact profile",
        "CI framing", "domain facts", "mimo-v2.5", "Qwen3.6",
        "individual call", "per-model", "mimo", "deepseek",
        "gradient scope", "graduated scope", "fact count",
        "domain sparsity", "batch inflation", "per-domain accuracy",
        "fact-dominance", "positional placement", "attentional redirection",
        "regressive facts", "numerical specificity", "counter-fact",
        "baseline threshold", "fact AFTER",
    ],
    "infrastructure": [
        "workspace", "janitor", "cleanup", "persistence", "garbage collection",
        "TTL", "archival", "disk", "file system", "workspace loss",
        "persistent workspace", "long-running experiment",
    ],
    "routing": [
        "routing", "OpenRouter", "batch inflation", "temperature-dependent",
        "routing layer", "routing overhead", "selective routing",
        "domain routing", "convergence", "minimum sample",
        "LR per-domain", "LR matrix", "domain count", "crossover",
        "Hybrid", "Hybrid first", "Stacked Hybrid", "LR=2", "LR failure",
        "class imbalance", "non-linearity", "Bayes ceiling",
        "adaptive pipeline", "static configuration",
    ],
    "quantization": [
        "INT1", "INT2", "INT4", "INT8", "cascade architecture",
        "quantization", "bit-width", "precision", "low-bit",
        "error correlation", "break-even",
    ],
    "feature_engineering": [
        "feature set", "feature category", "minimal feature",
        "XGBoost F1", "char_ratio", "lang_mismatch", "2-feature",
        "3-4 features", "feature ceiling", "type-dependent C",
        "regularization", "C=50", "C=100",
        "FEATURE_CATEGORY_ANALYSIS", "SIGNIFICANT_GAP",
    ],
    "cross_lingual_expanded": [
        "script-aware", "script-matched", "non-English", "CJK",
        "language mismatch", "character ratio", "FPR",
        "gate", "gating",
    ],
    "ai_safety_expanded": [
        "authority", "deference", "anti-deference", "authority-weighting",
        "authority weighting", "escalation", "conversation length",
        "multi-turn", "sycophancy", "amplifier",
    ],
    "meta_learning": [
        "MAML", "meta-learning", "cold start", "coldstart",
        "model-agnostic meta", "inner loop", "outer loop",
        "few-shot adaptation", "task distribution",
        "maml_", "maml vs", "maml cold",
    ],
    "centroid": [
        "centroid", "prototype", "class center", "nearest centroid",
        "production", "viable",
    ],
    "chemistry": [
        "chemistry", "chemical", "molecular", "compound",
        "reaction", "catalyst", "compound",
    ],
}


def classify_by_keywords(text):
    """Classify text into a domain using keyword matching."""
    text_lower = text.lower()

    # Try each domain's keywords, first match wins
    for domain, keywords in DOMAIN_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                return domain

    return None  # No match — leave as-is


def classify_experiment(exp):
    """Classify a single experiment by its hypothesis + result + ID."""
    hypothesis = exp.get("hypothesis", "") or ""
    result = exp.get("result", "") or ""
    exp_id = exp.get("id", "") or ""
    text = f"{exp_id} {hypothesis} {result}"
    return classify_by_keywords(text)


def backfill(dry_run=False, target_id=None):
    """Classify all uncategorized experiments."""
    db = get_db(DB_PATH)

    if target_id:
        rows = db.execute(
            "SELECT id, hypothesis, result, domain FROM experiments WHERE id = ?",
            (target_id,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, hypothesis, result, domain FROM experiments "
            "WHERE domain = 'uncategorized' OR domain = '' OR domain IS NULL"
        ).fetchall()

    print(f"Found {len(rows)} uncategorized experiments")

    classified = 0
    skipped = 0
    updates = []

    for exp_id, hypothesis, result, current_domain in rows:
        text = f"{exp_id} {hypothesis or ''} {result or ''}"
        domain = classify_by_keywords(text)

        if domain and domain != current_domain:
            updates.append((domain, exp_id))
            classified += 1
            if dry_run:
                print(f"  {exp_id}: -> {domain}  ({(hypothesis or '')[:60]})")
        else:
            skipped += 1

    print(f"\nWould classify: {classified}")
    print(f"No match (skip): {skipped}")

    if not dry_run and updates:
        db.executemany(
            "UPDATE experiments SET domain = ? WHERE id = ?",
            updates
        )
        db.commit()
        print(f"Updated {len(updates)} experiments")

    db.close()
    return classified


def main():
    parser = argparse.ArgumentParser(description="Auto-classify uncategorized experiments")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--id", type=str, help="Classify a single experiment by ID")
    args = parser.parse_args()

    backfill(dry_run=args.dry_run, target_id=args.id)


if __name__ == "__main__":
    main()
