#!/usr/bin/env python3
"""
Domain Taxonomy Merger — canonical resolution for fragmented experiment domains.

The experiment system accumulated 849 unique domain names for ~8500 experiments.
Many are variants of the same concept:
  "security" (1725) + "security-injection" (60) + "security/injection" (20) + ...

This script:
1. Identifies variant groups using keyword extraction + fuzzy matching
2. Merges variants into canonical domain names
3. Updates the prometheus.db experiments table
4. Reports before/after statistics

Usage:
    python3 domain_taxonomy_merge.py --dry-run     # Show what would change
    python3 domain_taxonomy_merge.py               # Execute merge
    python3 domain_taxonomy_merge.py --report       # Show current state
"""

import sqlite3
import os
import sys
import re
from collections import Counter, defaultdict
from db_retry import get_db

"""CLI tool: Domain Taxonomy Merge.

Usage: python3 domain_taxonomy_merge.py [options]
"""


DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")

# ─── Canonical domain mappings ───────────────────────────────────────────────
# Groups of domain names that should map to the same canonical name.
# The canonical name is the FIRST item in each group.
# Format: canonical -> [variants...]

CANONICAL_MAPPINGS = {
    # ── Security / Injection Detection (the big one) ──
    "security": [
        "security", "SECURITY", "Security", "ai-security", "ai_security",
        "security-detection", "security_detection", "security-ai",
        "security-inference", "security-ml", "security-pipeline",
        "security-text-analysis", "security-classification",
        "security-calibration", "security-classifier-transfer",
        "security-gradient-detection", "security-routing",
        "security-nlp-transfer", "security-adversarial-detection",
        "security-to-finance-transfer", "security-drift-detection",
        "security-injection", "security/injection",
        "security-injection-detection", "security/injection-detection",
        "security-injection-dedup", "security-injection-defense",
        "security-injection-routing", "security-prompt-injection",
        "security/prompt-injection", "security/prompt-injection-detection",
        "security-adversarial-nlp", "security/adversarial-nlp",
        "security-adversarial-ml", "security/adversarial-ml",
        "security-audio-adversarial", "security/audio-adversarial",
        "security-ensemble-learning", "security/ensemble_learning",
        "security-code-injection", "security/code-injection",
        "security-embeddings", "security/embeddings",
        "security-dedup", "security/dedup",
        "security-distillation", "security/distillation",
        "security-information-theory", "security/information-theory",
        "security-membership-inference", "security/membership-inference",
        "security-nlp", "security/nlp",
        "security-rag-hallucination", "security/rag-hallucination",
        "security-rag-robustness", "security/rag-robustness",
        "security-spectral-analysis", "security/spectral-analysis",
        "security-style-attribution", "security/style-attribution",
        "security-transfer-learning", "security/transfer-learning",
        "security-ai-defense", "security/ai-defense",
        "security/AI-detection", "security/AI-detection",
        "security-network-detection", "security_network_detection",
        "security-cross-domain", "security/fraud-detection,cross-domain-transfer",
        "security,scaling-laws,cross-domain-transfer",
        "security,fraud-detection,cross-domain-transfer",
        "security-text-classification/linguistic-typology",
        "security-injection-ensemble-methods,model-calibration,cross-lingual,adversarial-robustness",
        "security/injection-detection,ensemble-methods,model-calibration,cross-lingual,adversarial-robustness",
        "security,ensemble-methods,model-calibration,cross-lingual,adversarial-robustness",
        "security,prompt-injection,ensemble-methods,model-calibration,cross-lingual,adversarial-robustness",
        "security-prompt-injection-detection",
        "security-injection-detection-ensemble-methods,model-calibration,cross-lingual,adversarial-robustness",
        "security/injection-detection,ensemble-methods,model-calibration,cross_lingual,adversarial_robustness",
        "security-injection-detection-ensemble-methods,model-calibration,cross_lingual,adversarial_robustness",
    ],

    "injection_detection": [
        "injection_detection", "injection detection", "injection-detection",
        "injection-detectio", "injection detectio",
        "injection", "INJECTION", "Injection",
        "prompt-injection-detection", "prompt_injection_detection",
        "prompt-injection-d", "prompt-injection",
        "prompt_injection", "prompt injection",
        "nlp-injection", "nlp_injection", "nlp injection",
        "pre_injection_detection", "pre_injection_validation",
        "pre-injection-detection", "pre-injection-validation",
        "pre injection detection", "pre injection validation",
        "adversarial-detection", "adversarial_detection",
        "adversarial-detection-transfer",
        "adversarial-detection,embeddings,reconstruction-error",
        "adversarial-detection,real-time-security",
        "adversarial-detection/theoretical-foundations",
        "adversarial-detection-transfer-theoretical-foundations",
        "adversarial-text-detection", "adversarial-text",
        "adversarial-nlp", "adversarial-nlp-detection",
        "dialogue-injection-detection",
        "code-injection-detection", "code_injection_detection",
        "code-injection", "code_injection",
        "network-intrusion-detection", "network_intrusion_detection",
        "network-intrusion", "network_intrusion",
        "cjk-injection-detection", "cjk_injection_detection",
        "injection-detection-ensemble-methods,model-calibration,cross-lingual,adversarial-robustness",
        "injection_detection,ensemble_methods,model_calibration,cross_lingual,adversarial_robustness",
        "injection-detection,ensemble-methods,model-calibration,cross-lingual,adversarial-robustness",
        "injection_detection,synthesis",
        "injection-detection-defense",
        "injection-defense",
        "security-injection-detection-defense",
        "jailbreak-detection", "jailbreak-safety",
    ],

    "machine_learning": [
        "machine_learning", "machine learning", "machine-learning",
        "ML", "ml", "ML-safety", "ml-safety", "ML_safety", "ml_safety",
        "ML safety", "ML safety / adversarial robustness",
        "ML safety,adversarial robustness", "ML safety/model monitoring",
        "ML safety/monitoring", "ML stability/prompt-injection",
        "ML theory/scaling laws", "ML-benchmarking", "ML-features",
        "ML pipelines, software engineering, automation safety",
        "ML robustness, transfer learning, data augmentation theory",
        "ML/finetuning", "ML/toxicity-detection",
        "machine-learning-regularization", "machine-learning/toxicity-detection",
        "machine_learning,feature_universality,fine_tuning",
        "machine_learning/calibration",
        "ml-fundamentals", "ml-generalization", "ml-inference-optimization",
        "ml-mechanisms", "ml-monitoring,gradient-analysis", "ml-ops",
        "ml-pipeline-optimization", "ml-scaling-laws", "ml-theory",
        "ml-theory,capacity,overfitting", "ml-training",
        "ml-training,signal-analysis", "ml-training/fine-tuning",
        "ml-transfer-learning", "ml_general", "ml-ensemble-metalearning",
        "ml-ensemble-methods", "ml-ensemble",
        "mlops/training", "mlops",
        "production_ml", "production_optimization", "production",
        "inference_optimization", "inference-performance", "inference-optimization",
        "gpu-inference", "gpu_management",
        "optimization-theory", "speed-optimization",
    ],

    "nlp": [
        "nlp", "NLP", "NLP safety", "NLP-safety", "nlp-safety",
        "NLP classification", "NLP-classification", "nlp-classification",
        "NLP-detection", "nlp-detection", "NLP/RAG",
        "NLP/authorship_attribution", "NLP/short-text-classification",
        "NLP/style-analysis", "NLP/style-attribution/multilingual",
        "NLP/style-transfer", "NLP/text-classification",
        "NLP/text-style", "NLP/transfer-learning",
        "NLP/text-attribution", "nlp/text-classification",
        "nlp/text-style-analysis", "nlp/text-style-attribution",
        "nlp/text-classification", "nlp/classification",
        "nlp/embeddings", "nlp/hallucination", "nlp/hallucination_detection",
        "nlp/style-analysis", "nlp/style_attribution",
        "nlp/text-attribution", "nlp_classification",
        "nlp_preprocessing,embedding_search", "nlp_security",
        "nlp/text-attribution", "nlp/text-stylometry,authorship-attribution",
        "nlp,style-attribution,authorship", "nlp,style-transfer,authorship-attribution",
        "nlp,style_attribution", "nlp,stylometry,authorship-attribution",
        "nlp,text-stylometry,authorship-attribution",
        "nlp-adversarial", "nlp-augmentation", "nlp-cross-domain",
        "nlp-fine-tuning", "nlp-generalization", "nlp-hallucination-detection",
        "nlp-multilingual", "nlp-prompting", "nlp-robustness",
        "nlp-style-transfer-attribution", "nlp-tasks",
        "nlp-transfer,quantization,embeddings", "nlp-transfer-learning",
        "nlp/transfer-learning", "nlp/embeddings",
        "nlp,information-retrieval", "nlp-attribution",
        "nlp-embeddings", "nlp-hallucination", "nlp-preprocessing",
        "nlp-security", "nlp-transfer", "nlp-style-attribution",
        "nlp/text-attribution",
    ],

    "calibration": [
        "calibration", "LLM-calibration", "llm-calibration",
        "LLM calibration,overconfidence,selective routing safety",
        "LLM calibration/selective routing", "LLM-routing/calibration",
        "LLM-overconfidence/selective-routing", "LLM-overconfidence",
        "LLM-routing", "LLM_ROUTING", "llm-routing",
        "confidence_calibration", "calibration_selective_prediction",
        "selective_routing", "selective_compute_routing",
        "routing-optimization", "routing_calibration",
        "ai-calibration,selective-routing,llm-overconfidence",
        "auto_calibration", "curriculum_calibration",
        "distillation-calibration", "rlhf-calibration",
        "rlhf-distillation-calibration", "prompting-calibration",
    ],

    "ai_safety": [
        "ai-safety", "ai_safety", "AI-safety", "AI_safety",
        "AI Safety / Embedding Analysis", "AI Safety / LLM Security",
        "AI Safety/Calibration", "AI Safety/RLHF",
        "AI alignment/distillation", "AI safety", "AI safety / alignment",
        "AI safety / hallucination detection", "AI safety/RAG security",
        "AI safety/defense ensemble learning", "AI safety,cognitive-bias",
        "AI safety/feature-representation", "AI safety/calibration-monitoring",
        "AI safety/model-monitoring", "AI safety,peer-review,automation-vulnerability",
        "AI safety,authority deference,RLHF,non-LLM systems",
        "AI safety,authority deference,cross-system transfer",
        "AI safety,causal representation learning,adversarial robustness",
        "AI safety,distillation,RLHF", "AI safety,fake review detection",
        "AI safety,hallucination detection",
        "AI safety,multilingual RLHF,prompt injection",
        "AI safety,RLHF,authority-deference,non-LLM systems",
        "AI safety,RLHF,authority-deference,cross-system-generalization",
        "AI safety,RLHF,authority-deference,cross-system-transfer",
        "AI safety,RLHF,authority deference,non-LLM systems",
        "AI safety,RLHF,authority deference,cross-system-generalization",
        "AI-SAFETY", "AI-safety-detection",
        "ai-safety,ai-security", "ai-safety,authority-deference,non-llm-systems",
        "ai-safety,cross-system-authorization",
        "ai-safety,distillation,architecture-dependence",
        "ai-safety,transfer", "ai-safety,transfer-learning,authority-deference",
        "ai-safety,rlhf,authority-deference,cross-system-transfer",
        "ai-safety,rlhf,authority-deference,cross-system-generalization",
        "ai-safety,rlhf,authority-deference",
        "ai-safety,rlhf,authority-deference,non-llm-systems",
        "ai-safety,transfer-learning,authority-deference",
        "ai-safety/alignment", "ai-safety/authority-deference",
        "ai-alignment", "alignment", "alignment/distillation",
        "alignment/rlhf", "ai-safety,transfer-learning",
        "ai-safety,transfer",
        "ai_safety,ai_alignment,rlhf", "ai_safety,rlhf,authority_deference",
        "ai_safety/adversarial", "ai_safety/rlhf",
        "ai_alignment",
    ],

    "cross_domain_transfer": [
        "cross_domain_transfer", "cross-domain-transfer",
        "cross-domain", "cross_domain", "cross domain",
        "cross-domain-synthesis", "cross_domain_recovery",
        "cross-experiment-synthesis", "cross_cutting-synthesis",
        "cross-cutting-synthesis", "cross-feature-type-transfer",
        "cross-domain-transfer,prompt-injection",
        "cross_domain_immunity",
        "multi-domain", "multi_domain", "multi-domain-transfer",
        "multi-domain LLM review", "multi-domain-regulatory",
        "multi_domain_regulatory",
    ],

    "ensemble_methods": [
        "ensemble_methods", "ensemble-methods", "ensemble methods",
        "ensemble", "ensemble-learning", "ensemble_learning",
        "ensemble-learning,cognitive-bias", "ensemble-detection",
        "ensemble_detection", "ensemble-theory", "ensemble_theory",
        "ensemble-systems", "ensemble-reasoning",
        "ensemble-analysis", "ensemble-selection",
        "ensemble-selection,meta-learning",
        "ensemble-distillation", "ensemble_distillation",
        "ensemble-pruning", "ensemble_pruning",
        "ensemble-theory", "ensemble_learning,machine_learning",
        "ensemble_detection,encoding_attacks,feature_representation,cross_domain_robustness",
        "ensemble-learning,cognitive-biases",
        "ml-ensembles", "ml-ensemble", "ml-ensemble-metalearning",
        "ml-ensemble-methods",
    ],

    "adversarial_ml": [
        "adversarial-ml", "adversarial_ml", "adversarial ML",
        "adversarial-machine-learning", "adversarial-robustness",
        "adversarial_robustness", "adversarial-robustness,detection",
        "adversarial-ml-audio", "adversarial-ml-cross-modality",
        "adversarial-ml-defense", "adversarial-ml/calibration-detection",
        "adversarial-ml/security", "adversarial-ml,rag-safety",
        "adversarial-ml,vision-security",
        "adversarial", "adversarial-attacks", "adversarial-attacks,modality",
        "adversarial-audio", "adversarial-audio-detection",
        "adversarial-audio, cross-modal-transfer",
        "adversarial-embeddings", "adversarial-images",
        "adversarial-ml,rag-safety",
        "adversarial-security", "adversarial_vision",
        "adversarial-vision", "adversarial-vision,adversarial-nlp,cross-domain-synthesis",
        "adversarial-vision-adversarial-nlp-cross-domain-synthesis",
        "adversarial-attack", "adversarial_defense",
        "adversarial_detection,audio,speech,calibration",
        "adversarial_detection,production_filtering",
        "adversarial_detection,prompt_injection,embeddings",
        "adversarial_embedding", "adversarial_training",
        "adversarial-ml,vision-security",
        "adversarial-attacks", "adversarial-audio-detection",
    ],

    "agent_systems": [
        "agent_systems", "agent-systems", "agent systems",
        "agent_architecture", "agent_behavior",
        "multi-agent", "multi_agent", "multi-agent-systems",
        "multi-agent-llm", "multi-agent-llm-consensus",
        "multi-agent-LLM-consensus", "multi-agent-consensus",
        "agi-loop", "agi_cognition",
    ],

    "adversarial_detection": [
        "adversarial-detection", "adversarial_detection",
        "adversarial-detection-transfer",
        "adversarial-detection,embeddings,reconstruction-error",
        "adversarial-detection,real-time-security",
        "adversarial-detection/theoretical-foundations",
        "adversarial-text-detection",
        "detection-routing", "detection-theory", "detection_mechanisms",
        "detection",
    ],

    "embedding": [
        "embedding", "embeddings", "embedding_methods", "embedding-analysis",
        "embedding-transfer", "embeddings-kd",
        "embedding-fact-checking", "embedding-semantics",
        "kg_embeddings", "representation-learning",
        "representation-learning,mechanistic-interpretability",
        "representation-learning,training-objectives,predictability",
        "metric_learning", "embedding-structure",
        "vocabulary_alignment",
    ],

    "safety": [
        "safety", "safety-classifiers", "safety-classification",
        "safety-classifier-transfer", "safety-detection",
        "safety-ensemble", "safety-calibration",
        "safety-combination", "safety/safety-classification",
        "safety/classification", "safety/classifier",
        "safety/sycophancy", "safety-classification",
        "ml-safety", "ML-safety", "nlp-safety", "NLP-safety",
        "content_moderation", "moderation",
    ],

    "meta_analysis": [
        "meta_analysis", "meta-analysis", "meta analysis",
        "meta-analysis/exploration-bias", "meta-attack",
        "meta-experiment-audit", "meta-experimentation",
        "meta-experiment", "meta-experiment-analysis",
        "meta-research", "meta_research", "meta-learning",
        "meta_learning", "META-LEARNING", "meta-science",
        "meta-synthesis", "meta-evaluation", "meta_evaluation",
        "meta-cognition", "meta_cognition", "metacognition",
        "metascience", "meta", "META", "meta-pipeline",
        "meta-analysis,exploration-bias",
        "meta-learning,ensemble-selection,dynamic-fusion",
        "meta-learning,model-selection,transfer",
        "meta-learning,prediction",
        "pipeline_meta", "research-methodology",
        "ai_research_methodology",
    ],

    "hallucination_detection": [
        "hallucination-detection", "hallucination_detection",
        "hallucination", "HALLUCINATION",
        "rag-hallucination-detection", "rag/hallucination",
        "RAG hallucination detection", "RAG,hallucination,embeddings",
        "nlp-hallucination-detection",
    ],

    "rag": [
        "rag", "RAG", "rag_dedup", "rag-dedup", "rag-deduplication",
        "rag-systems", "rag_systems", "RAG-systems",
        "rag-safety", "rag_safety", "rag-security",
        "rag-analysis", "rag_analysis", "rag-context",
        "rag-detection", "rag-interference",
        "nlp/RAG/attention-mechanisms",
        "attention-mechanisms,RAG", "attention-mechanisms/RAG",
    ],

    "audio_security": [
        "audio-security", "audio_security",
        "audio-adversarial-detection", "audio_adversarial_detection",
        "audio-adversarial", "adversarial-audio",
        "adversarial-audio-detection", "adversarial-audio-attacks",
        "audio-injection-detection",
        "audio-security,adversarial-detection",
        "audio-security,cross-domain-transfer",
        "audio-security,detection-methods",
        "audio-adversarial-ml-safety", "audio-adversarial-security",
        "audio adversarial detection",
        "audio,adversarial-attacks,modality",
    ],

    "hardware": [
        "hardware", "hardware_ml", "edge_deployment",
        "edge_inference",
    ],

    "tfidf": [
        "tfidf", "tfidf_lr", "TF-IDF", "tf-idf",
    ],

    "cross_pollination": [
        "cross_pollination", "cross-pollination",
    ],

    "data_quality": [
        "data_quality", "data-quality",
    ],

    "attack": [
        "attack", "adversarial-attack",
    ],

    "defense": [
        "defense", "defense_mechanisms",
        "multi-turn-defense", "multi_turn_defense",
    ],

    "network_security": [
        "network_security", "network-security",
        "network-intrusion-detection", "network_intrusion_detection",
        "network-intrusion", "network_intrusion",
        "network-obfuscation", "network_obfuscation",
        "network-obfuscation-detection", "network_obfuscation_detection",
        "network-packet-analysis", "network_packet_analysis",
        "nids",
    ],

    "financial_fraud_detection": [
        "financial-fraud-detection", "financial_fraud_detection",
        "financial-fraud", "financial_fraud",
        "fraud-detection", "fraud_detection",
    ],

    "code_analysis": [
        "code-analysis", "code_analysis",
        "code-analysis,detection", "code-analysis,transfer-learning",
        "code-security", "code_security",
        "code-analysis,detection",
    ],

    "medical_nlp": [
        "medical-nlp", "medical_nlp",
        "medical-NLP", "medical-NLP,contrastive-learning",
        "medical-nlp,few-shot-learning,meta-learning",
        "medical-ai", "medical_ai",
        "medical-embeddings", "medical_embeddings",
        "medical-ner", "medical-text",
        "medical-diagnosis", "medical_diagnosis",
        "medical", "medical_moe_interference",
    ],

    "vision": [
        "vision", "vision-security",
        "vision-adversarial", "vision_adversarial_detection",
        "computer vision, synthetic-to-real transfer",
        "image-adversarial-detection", "image-adversarial-ml",
        "image-classification", "image_classification",
        "multimodal-vqa", "multimodal VQA",
        "multimodal-vqa-hallucination",
        "multimodal-vqa,hallucination-detection",
        "multimodal-vqa-hallucination",
        "multimodal-adversarial-robustness",
        "multimodal-security", "multimodal-detection",
        "multimodal-detection",
    ],

    "distillation": [
        "distillation", "knowledge-distillation",
        "ml-distillation", "distillation, RLHF, calibration",
        "distillation,RLHF,calibration",
        "distillation,RLHF,architecture-dependence",
        "distillation/RLHF/calibration",
        "distillation-calibration",
        "data-free-distillation",
    ],

    "rlhf": [
        "RLHF", "rlhf", "rlhf-calibration",
        "rlhf-distillation-calibration",
        "rlhf,calibration,distillation",
        "rlhf,distillation,architecture-dependence",
        "rlhf,calibration,distillation",
        "rlhf,distillation,architecture-dependence",
        "rlhf,calibration,distillation",
        "rlhf,calibration,distillation",
    ],

    "scaling_laws": [
        "scaling-laws", "scaling_laws",
        "scaling-laws, ML-prediction",
        "scaling-laws, compute-prediction, regression",
        "scaling-laws,LLM-training",
        "scaling-laws",
    ],

    "interpretability": [
        "interpretability", "interpretability/probing",
        "mechanistic-interpretability",
        "attention", "attention-mechanisms",
        "attention-visualization", "attention_visualization",
        "attention-transfer", "attention_transfer",
        "attention-pooling", "attention_pooling",
        "probing-and-representations",
    ],

    "llm_training": [
        "LLM training/RLHF", "LLM training/distribution",
        "llm_fine_tuning", "llm-training",
        "llm-prompting", "llm_prompting",
        "llm-judge", "llm_judge",
        "LLM evaluation, transfer learning",
        "LLM factual recall", "LLM fine-tuning, representation analysis",
        "LLM routing safety", "LLM routing/safety",
        "LLM-in-the-loop pipelines", "llm-in-the-loop-pipelines",
        "LLM-attention", "LLM-ensemble,statistical-voting",
        "instruction-following",
    ],

    "cross_lingual": [
        "cross_lingual", "cross-lingual",
        "cross_lingual_transfer", "cross-lingual-transfer",
        "multilingual", "multilingual-nlp", "multilingual-safety",
        "multilingual_nlp", "multilingual injection detection",
        "non_english_classification",
        "unicode",
    ],

    "text_classification": [
        "text-classification", "text_classification",
        "text-detection", "text_detection",
        "text-style", "text_style_attribution",
        "text-generation",
    ],

    "style_attribution": [
        "style-attribution", "text-style-attribution",
        "text-style", "text_style_attribution",
        "nlp/style-attribution", "nlp/style_attribution",
        "nlp/text-style-attribution", "nlp/text-attribution",
        "nlp-authorship-attribution",
        "nlp,style-attribution,authorship",
        "nlp,style-transfer,authorship-attribution",
        "nlp,style_attribution",
        "nlp,stylometry,authorship-attribution",
        "nlp,text-stylometry,authorship-attribution",
        "nlp/text-style-analysis", "nlp/text-style-attribution",
        "authorship", "authorship-attribution",
        "text-style", "nlp-style-attribution",
        "nlp-style-transfer-attribution",
    ],

    "sycophancy": [
        "sycophancy",
    ],

    "social_engineering": [
        "social_engineering", "social-engineering",
        "social_engineering_detection",
        "phishing_detection", "phishing-detection",
    ],

    "regulatory": [
        "regulatory", "regulatory_domains",
        "regulatory_compliance", "EU AI Act", "FDA",
    ],

    "dedup_pipeline": [
        "dedup-pipeline", "dedup_pipeline",
        "dedup-pipeline", "dedup-pipeline",
        "dedup-pipeline", "dedup-pipeline",
    ],

    "model_monitoring": [
        "model-monitoring", "model_monitoring",
        "model-health", "model-health-monitoring",
        "model-health-monitoring",
        "drift-detection", "drift-detection",
        "monitoring",
    ],

    "workspace": [
        "workspace", "workspace-management",
    ],

    "dispatch": [
        "dispatch", "DISPATCH", "PLATFORM-DISPATCH",
        "platform", "PLATFORM", "PLATFORM-DISPATCH",
        "content-routing", "routing",
    ],

    "uncategorized": [
        "uncategorized", "other", "DICT", "PLATFORM",
        "GENERAL", "META", "DISPATCH", "CODE",
        "PLATFORM-DISPATCH", "DICT-PLATFORM",
        "general", "all", "test", "domain",
        "implementation", "systems", "architecture",
        "methodology", "methodology",
    ],
}


def load_db():
    """Load experiment domains from prometheus.db."""
    return get_db(DB_PATH)


def get_domain_counts(db):
    """Get all domains and their counts."""
    rows = db.execute(
        "SELECT domain, COUNT(*) as cnt FROM experiments WHERE domain IS NOT NULL GROUP BY domain ORDER BY cnt DESC"
    ).fetchall()
    return {domain: count for domain, count in rows}


def build_reverse_map():
    """Build reverse mapping: variant -> canonical."""
    reverse = {}
    for canonical, variants in CANONICAL_MAPPINGS.items():
        for variant in variants:
            reverse[variant] = canonical
    return reverse


def merge_domains(db, dry_run=True):
    """Merge variant domain names into canonical names."""
    reverse_map = build_reverse_map()
    domain_counts = get_domain_counts(db)

    merges = {}  # canonical -> [variants to merge]
    unmapped = []

    for domain, count in domain_counts.items():
        canonical = reverse_map.get(domain)
        if canonical and canonical != domain:
            if canonical not in merges:
                merges[canonical] = []
            merges[canonical].append((domain, count))
        elif not canonical:
            unmapped.append((domain, count))

    total_merged = sum(count for variants in merges.values() for _, count in variants)

    if dry_run:
        print("=" * 70)
        print("DOMAIN TAXONOMY MERGE (DRY RUN)")
        print("=" * 70)
        print(f"Total unique domains: {len(domain_counts)}")
        print(f"Domains to merge: {sum(len(v) for v in merges.values())}")
        print(f"Experiments affected: {total_merged}")
        print()

        for canonical, variants in sorted(merges.items(), key=lambda x: -sum(c for _, c in x[1])):
            variant_total = sum(c for _, c in variants)
            print(f"  {canonical} (+{variant_total} from {len(variants)} variants):")
            for variant, count in sorted(variants, key=lambda x: -x[1])[:10]:
                print(f"    +{count:5d}  {variant}")
            if len(variants) > 10:
                print(f"    ... and {len(variants) - 10} more")
            print()

        if unmapped:
            print(f"\nUnmapped domains ({len(unmapped)}, {sum(c for _, c in unmapped)} experiments):")
            for domain, count in sorted(unmapped, key=lambda x: -x[1])[:20]:
                print(f"  {count:5d}  {domain}")
            if len(unmapped) > 20:
                print(f"  ... and {len(unmapped) - 20} more")

        return merges, unmapped

    # Execute merge
    print("Executing merges...")
    for canonical, variants in merges.items():
        for variant, count in variants:
            db.execute(
                "UPDATE experiments SET domain = ? WHERE domain = ?",
                (canonical, variant)
            )
            print(f"  {variant} -> {canonical} ({count} experiments)")

    db.commit()
    print(f"\nMerged {len(merges)} canonical domains.")

    # Report final state
    final = get_domain_counts(db)
    print(f"Final unique domains: {len(final)}")
    print(f"\nTop 20 domains after merge:")
    for domain, count in sorted(final.items(), key=lambda x: -x[1])[:20]:
        print(f"  {count:5d}  {domain}")

    return merges, unmapped


def print_report(db):
    """Print current domain state."""
    domain_counts = get_domain_counts(db)
    total = sum(domain_counts.values())

    print("=" * 70)
    print("DOMAIN TAXONOMY REPORT")
    print("=" * 70)
    print(f"Total unique domains: {len(domain_counts)}")
    print(f"Total experiments: {total}")
    print()

    print("TOP 30 DOMAINS:")
    for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1])[:30]:
        pct = count / total * 100
        bar = "#" * min(40, int(pct * 2))
        print(f"  {count:5d} ({pct:5.1f}%)  {domain}  {bar}")

    # Count domains with only 1 experiment
    singletons = sum(1 for c in domain_counts.values() if c == 1)
    print(f"\nDomains with 1 experiment: {singletons} ({singletons/len(domain_counts)*100:.0f}%)")

    # Identify potential duplicates
    reverse_map = build_reverse_map()
    mapped = sum(1 for d in domain_counts if d in reverse_map and reverse_map[d] != d)
    print(f"Domains that would be merged: {mapped}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Domain Taxonomy Merger")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    parser.add_argument("--report", action="store_true", help="Show current state")
    args = parser.parse_args()

    db = load_db()

    if args.report:
        print_report(db)
    else:
        merge_domains(db, dry_run=args.dry_run)

    db.close()


if __name__ == "__main__":
    main()
