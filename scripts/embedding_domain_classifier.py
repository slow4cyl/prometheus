#!/usr/bin/env python3
"""embedding_domain_classifier.py — Embedding-based domain classification.

Uses qwen3-embedding-0.6b (port 9150, 1024-d) to classify experiment titles by
cosine similarity against canonical domain descriptions.

Backend priority:
1. qwen3-embedding-0.6b (port 9150) — the shared RAG embedding server
2. Regex-based fallback from domain_classifier.py

(The old ONNX bge-small-en :9151 fast-path was removed 2026-07-06 — its 384-d
vectors were incompatible with the 1024-d Qwen3 centroids.)

DESCRIPTION SYSTEM (two-layer):
- Hard-coded: DOMAIN_DESCRIPTIONS dict in this file. Hand-crafted, takes priority.
- Auto-generated: ~/.hermes/domain_auto_descriptions.json. Created by
  auto_extend_descriptions() which is called by domain_merge_sync.py (cron every 5m).
  New domain labels with >=5 experiments get centroids generated from their
  experiment hypothesis text automatically.

Usage:
    from embedding_domain_classifier import classify_embedding
    domain = classify_embedding("Can TF-IDF detect prompt injection?")
    # Returns: ('injection_detection', 0.664)

    # Batch classify
    from embedding_domain_classifier import classify_batch
    results = classify_batch(["title1", "title2", ...])

    # Auto-extend centroids for new domains
    from embedding_domain_classifier import auto_extend_descriptions
    added = auto_extend_descriptions(min_experiments=5)
"""
import os
import re
import sys
import json
import numpy as np
from typing import Optional, List, Tuple

# Import regex-based fallback
sys.path.insert(0, os.path.dirname(__file__))
from domain_classifier import classify as classify_regex

# ONNX backend (preferred — sub-5ms latency)
# ONNX bge-small-en (:9151) retired 2026-07-05 — 384-d, incompatible with the 1024-d Qwen3 centroids.

# Legacy backend
EMBEDDING_URL = "http://localhost:9150/v1/embeddings"
EMBEDDING_MODEL = "qwen3-embedding-0.6b"

SIMILARITY_THRESHOLD = 0.35  # Minimum cosine similarity to classify

# Backend selection
ACTIVE_BACKEND = None  # Will be auto-detected on first use

# Canonical domain descriptions — these are what we embed and compare against
DOMAIN_DESCRIPTIONS = {
    "injection_detection": "prompt injection detection, jailbreak detection, adversarial prompt attacks, LLM security, input sanitization, safety filters, content moderation, harmful content detection",
    
    "adversarial_ml": "adversarial machine learning, adversarial attacks, adversarial robustness, perturbation attacks, evasion attacks, FGSM, PGD, adversarial examples, model hardening",
    
    "cross_domain_prediction": "cross-domain transfer, transfer learning, domain adaptation, cross-domain generalization, analogical reasoning, knowledge transfer between fields, transferability of mechanisms",
    
    "tfidf": "TF-IDF, term frequency inverse document frequency, text classification, feature extraction, lexical analysis, bag of words, text mining, NLP features",
    
    "calibration": "model calibration, confidence calibration, probability estimation, temperature scaling, Platt scaling, uncertainty quantification, prediction confidence, reliability diagrams",
    
    "dim_reduction": "dimensionality reduction, PCA, SVD, feature selection, subspace methods, manifold learning, t-SNE, UMAP, feature extraction, latent space",
    
    "metrics_benchmarking": "evaluation metrics, benchmarking, F1 score, precision, recall, accuracy, AUC, model comparison, performance evaluation, statistical testing",
    
    "optimization": "hyperparameter optimization, model optimization, training optimization, learning rate scheduling, gradient descent, convergence, efficiency optimization, resource optimization",
    
    "physical_dynamics": "physical systems, mechanical dynamics, thermodynamics, fluid dynamics, heat transfer, materials science, structural mechanics, wave propagation, acoustic systems",
    
    "temperature_analysis": "temperature effects, thermal analysis, heat distribution, thermal management, temperature sensitivity, thermal properties, temperature-dependent behavior",
    
    "regulatory": "regulatory compliance, GDPR, HIPAA, Basel III, COPPA, EU AI Act, privacy regulations, compliance frameworks, legal requirements, audit",
    
    "ensemble_methods": "ensemble methods, model ensembles, random forests, gradient boosting, bagging, stacking, model combination, ensemble diversity, multi-model systems",
    
    "attention": "attention mechanisms, transformer attention, self-attention, multi-head attention, attention patterns, attention visualization, attention-based models",
    
    "meta_research": "meta-analysis, meta-research, research methodology, experiment design, scientific methodology, research synthesis, systematic review, evidence aggregation",
    
    "lexical": "lexical analysis, word-level features, character n-grams, text attribution, stylometry, writing style, linguistic features, vocabulary analysis",
    
    "safety": "AI safety, alignment, value alignment, safety testing, red teaming, harm prevention, safety evaluation, constitutional AI, safety benchmarks",
    
    "cost_analysis": "cost analysis, resource utilization, computational cost, API cost, token cost, efficiency metrics, cost-benefit analysis, resource optimization",
    
    "dispatch_pipeline": "dispatch system, pipeline architecture, task routing, workflow automation, task scheduling, pipeline optimization, orchestration",
    
    "dict_methodology": "dictionary methodology, fact injection, knowledge injection, domain-specific facts, fact sets, prompt engineering with facts, contextual information",
    
    "llm_general": "LLM evaluation, language model benchmarking, model comparison, prompt engineering, chain-of-thought, in-context learning, few-shot learning",
    
    "bias": "bias detection, fairness, demographic parity, equal opportunity, bias mitigation, algorithmic fairness, protected attributes, bias quantification",
    
    "system_infrastructure": "system infrastructure, deployment, monitoring, logging, observability, scaling, distributed systems, reliability, fault tolerance",
    
    "agent_systems": "AI agents, autonomous agents, tool use, agent architectures, multi-agent systems, agent planning, agent reasoning, agent evaluation",

    # === NEW SCIENTIFIC DOMAINS (June 7 2026) ===
    # Cross-domain and general
    "cross_domain": "cross-domain transfer, cross-domain prediction, analogical reasoning, knowledge transfer between scientific fields, mechanism transfer, bilateral edges",
    "transfer_learning": "transfer learning, domain adaptation, few-shot learning, data augmentation, cross-domain generalization",
    "general": "general science, unclassified research, mixed methodology, multi-domain analysis",

    # Biology and life sciences
    "biology": "biology, biological systems, organisms, species, evolution, genetics, ecology, cellular biology, molecular biology, physiology, anatomy",
    "biophysics": "biophysics, protein structure, protein folding, protein dynamics, structural biology, molecular biophysics, biophysical mechanisms, protein function",
    "tardigrade_biology": "tardigrade biology, tardigrade survival, cryptobiosis, anhydrobiosis, extreme environment tolerance, tardigrade physiology, tun state",
    "neuroscience": "neuroscience, brain, neurons, neural circuits, synaptic plasticity, cognitive neuroscience, computational neuroscience, brain imaging",
    "cognitive_science": "cognitive science, cognition, reasoning, decision making, memory, attention, perception, problem solving, mental models, cognitive architecture, working memory",
    "biomechanics": "biomechanics, muscle mechanics, joint mechanics, gait analysis, movement science, tissue mechanics, skeletal mechanics",
    "marine_biology": "marine biology, ocean organisms, coral reefs, marine ecology, ocean ecosystems, marine species, deep sea biology",
    "entomology": "entomology, insect biology, insect behavior, insect ecology, arthropods, pollination, insect physiology",

    # Materials and engineering
    "materials_science": "materials science, alloys, polymers, ceramics, composites, crystal structures, fracture mechanics, fatigue, corrosion, materials properties, manufacturing",
    "engineering": "engineering, structural engineering, civil engineering, mechanical engineering, electrical engineering, systems engineering, infrastructure",
    "semiconductor_physics": "semiconductor physics, semiconductor manufacturing, microelectronics, chip fabrication, transistor physics, wafer processing",
    "glassblowing": "glassblowing, glass science, glass properties, vitreous materials, glass forming, thermal properties of glass",

    # Earth and environmental sciences
    "ecology": "ecology, ecosystems, biodiversity, conservation, climate science, environmental science, habitat, population dynamics, species interactions",
    "physics": "physics, thermodynamics, quantum mechanics, fluid dynamics, wave physics, optics, acoustics, classical mechanics, electromagnetism",
    "chemistry": "chemistry, chemical reactions, molecular chemistry, organic chemistry, inorganic chemistry, analytical chemistry, chemical properties",
    "oceanography": "oceanography, ocean science, marine geology, ocean currents, sea level, ocean temperature, marine sediments",
    "astrophysics": "astrophysics, astronomy, stellar physics, cosmology, planetary science, galactic astronomy, observational astronomy",
    "geophysics": "geophysics, earth physics, seismic waves, gravity, magnetic fields, earth structure, geophysical surveying",
    "volcanology": "volcanology, volcanic eruptions, magma dynamics, volcanic hazards, geothermal systems, volcanic monitoring",
    "geomorphology": "geomorphology, landform evolution, erosion, sedimentation, landscape dynamics, river morphology, glacial geomorphology",
    "soil_science": "soil science, soil properties, soil chemistry, soil physics, soil ecology, soil classification, soil health",
    "atmospheric_science": "atmospheric science, meteorology, weather prediction, atmospheric dynamics, climate modeling, atmospheric chemistry",
    "seismology": "seismology, earthquake science, seismic hazard, wave propagation, fault mechanics, seismic monitoring",
    "paleontology": "paleontology, fossil record, ancient life, extinction events, evolutionary history, geological time",

    # Medical and health
    "medicine": "medicine, clinical research, patient outcomes, disease mechanisms, treatment efficacy, drug development, medical diagnostics, healthcare",
    "cardiology": "cardiology, heart disease, cardiac function, cardiovascular system, heart failure, arrhythmia, cardiac imaging",
    "pharmacology": "pharmacology, drug action, pharmacokinetics, pharmacodynamics, drug delivery, receptor binding, drug metabolism",

    # Urban and systems
    "urban_systems": "urban systems, city infrastructure, urban planning, urban ecology, urban climate, urban energy, urban health, smart cities",
    "urban_planning": "urban planning, city design, land use, transportation planning, zoning, urban development, metropolitan planning",
    "complex_systems": "complex systems, network science, emergent behavior, self-organization, phase transitions, power laws, cascade dynamics, resilience",
    "energy_systems": "energy systems, renewable energy, power grids, energy storage, energy efficiency, energy policy, distributed energy",
    "infrastructure": "computing infrastructure, server infrastructure, network infrastructure, cloud infrastructure, deployment infrastructure, resource provisioning, infrastructure management, system operations",
    "infrastructure_expanded": "large-scale infrastructure, infrastructure scaling, distributed infrastructure, multi-region deployment, capacity planning, infrastructure optimization, expanded operations",

    # ML and AI (expanded)
    "machine_learning": "machine learning, neural networks, deep learning, transformers, LLMs, quantization, hyperparameter tuning, training dynamics, model optimization",
    "ml_security": "ML security, adversarial attacks, adversarial robustness, prompt injection, jailbreak detection, AI safety, model hardening, adversarial examples",
    "ml_theory": "ML theory, learning theory, generalization, optimization theory, loss landscapes, gradient dynamics, model capacity, overfitting",
    "nlp": "natural language processing, text analysis, language models, tokenization, parsing, sentiment analysis, named entity recognition, text classification, sequence labeling, machine translation",
    "security": "cybersecurity, network security, intrusion detection, malware analysis, threat detection, vulnerability assessment, penetration testing, access control, cryptography, security auditing",
    "network_security": "network security, network protocols, secure communication, network monitoring, firewall, intrusion prevention, DDoS detection, traffic analysis, network defense",
    "audio_security": "audio security, speech recognition security, voice authentication, audio adversarial attacks, speaker verification, audio deepfakes, acoustic attacks, voice spoofing detection",

    # Information and data sciences
    "information_theory": "information theory, entropy, mutual information, KL divergence, channel capacity, data compression, rate-distortion theory, coding theory, Shannon theory, information bottleneck, Fisher information",
    "data_quality": "data quality, data validation, data cleaning, data profiling, data governance, data consistency, data completeness, data accuracy, data freshness, data reliability, data lineage",
    "code_analysis": "code analysis, static analysis, dynamic analysis, program comprehension, code quality, bug detection, code review, software metrics, code complexity, vulnerability detection",
    "causal_inference": "causal inference, causality, causal reasoning, causal discovery, causal effect estimation, do-calculus, counterfactual reasoning, structural causal models, treatment effect estimation, causal graphs",
    "vision": "computer vision, image recognition, object detection, image segmentation, visual recognition, face recognition, medical imaging, video analysis, scene understanding, visual reasoning",

    # Specialized domains
    "mycology": "mycology, fungi, fungal biology, mushroom science, fungal ecology, mycorrhizae, fungal genetics",
    "lacemaking": "lacemaking, lace production, textile arts, lace patterns, bobbin lace, needle lace, lace machinery",
    "woodcarving": "woodcarving, wood science, timber properties, woodworking, wood anatomy, wood durability, carved structures",
    "tanning": "tanning, leather processing, hide chemistry, leather properties, tannin chemistry, leather durability",
    "linguistics": "linguistics, phonetics, phonology, syntax, morphology, language typology, language acquisition, sociolinguistics, computational linguistics",
    "graduated_scope": "graduated scope, three-tier system, scope levels, progressive testing, tiered validation, graduated testing methodology, scope governance",
    "jailbreak": "jailbreak, LLM jailbreak, adversarial prompting, prompt injection, refusal bypass, safety bypass, harmful content generation, model exploitation, red teaming",

    # === Domains added during full classification audit (June 8 2026) ===
    # These existed in the DB but were missing from the classifier vocab, so
    # they were at risk of being erased by a full reclassify. Added to preserve.
    "interpretability": "interpretability, explainability, feature attribution, saliency maps, model transparency, mechanistic interpretability, probing classifiers, concept activation, neuron analysis, SHAP, LIME",
    "finance": "finance, financial markets, asset pricing, risk modeling, volatility, portfolio optimization, trading, credit risk, fraud detection, economic forecasting",
    "network_science": "network science, graph theory, complex networks, node centrality, community detection, network topology, scale-free networks, percolation, epidemic spreading on networks",
    "biochemistry": "biochemistry, enzyme kinetics, metabolic pathways, protein chemistry, molecular biology, reaction mechanisms, biomolecules, cellular biochemistry",
    "medical_nlp": "medical natural language processing, clinical text, electronic health records, biomedical text mining, clinical notes, medical entity extraction, ICD coding",
    "meta_analysis": "meta-analysis, systematic review, evidence synthesis, effect size aggregation, study heterogeneity, research methodology, pooled estimates, publication bias",
    "millinery": "millinery, hat making, headwear construction, hat blocking, brim shaping, felt and straw hats, milliner techniques",
    "cobbling": "cobbling, shoemaking, footwear construction, shoe repair, leather footwear, sole attachment, last shaping, cordwaining",
    "coppersmithing": "coppersmithing, copper metalwork, sheet metal forming, copper vessel making, annealing copper, raising and planishing, metal hammering",
    "saddlery": "saddlery, saddle making, equestrian leatherwork, harness making, leather tooling, stitching tack, riding equipment construction",
    "pottery": "pottery, ceramics, clay forming, glaze chemistry, kiln firing, wheel throwing, ceramic materials, pottery techniques",
    
    # Uncategorized catch-all
    "uncategorized": "miscellaneous, unclassified, general analysis, mixed topics, multi-domain research, cross-cutting analysis, interdisciplinary research",
}

# Cache embeddings
_domain_embeddings = None

# Auto-generated descriptions (extended by cron, not hard-coded)
AUTO_DESCRIPTIONS_PATH = os.path.expanduser("~/.hermes/domain_auto_descriptions.json")


def load_auto_descriptions() -> dict:
    """Load auto-generated domain descriptions from file."""
    if not os.path.exists(AUTO_DESCRIPTIONS_PATH):
        return {}
    try:
        with open(AUTO_DESCRIPTIONS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_auto_descriptions(descriptions: dict):
    """Save auto-generated domain descriptions to file."""
    try:
        with open(AUTO_DESCRIPTIONS_PATH, "w") as f:
            json.dump(descriptions, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save auto descriptions: {e}")


def load_all_descriptions() -> dict:
    """Merge hard-coded DOMAIN_DESCRIPTIONS with auto-generated ones.

    Auto-generated descriptions extend the hard-coded set and ARE
    persisted across restarts. Hand-crafted entries take priority
    (they have better semantic precision).
    """
    descriptions = dict(DOMAIN_DESCRIPTIONS)
    auto = load_auto_descriptions()
    for domain, desc in auto.items():
        if domain not in descriptions:
            descriptions[domain] = desc
    return descriptions


def generate_domain_description(domain: str, max_fragments: int = 20) -> str:
    """Generate a text description for a domain from its experiment hypotheses.

    Queries prometheus.db for experiments in this domain, extracts unique
    hypothesis fragments, and constructs a comma-separated description string.
    Used for auto-extending the embedding centroid cache.
    """
    import sqlite3
    hermes = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    db_path = os.path.join(hermes, "prometheus.db")
    if not os.path.exists(db_path):
        return domain.replace("_", " ") + ", " + domain.replace("_", " ") + " research"

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        c = conn.cursor()
        c.execute("SELECT hypothesis FROM experiments WHERE domain=? AND hypothesis IS NOT NULL AND hypothesis != ''",
                  (domain,))
        rows = c.fetchall()
        conn.close()
    except Exception:
        return domain.replace("_", " ") + ", " + domain.replace("_", " ") + " research"

    if not rows:
        return domain.replace("_", " ") + ", " + domain.replace("_", " ") + " research"

    # Collect unique hypothesis fragments, cleaned
    fragments = set()
    for (hypothesis,) in rows:
        # Strip transfer markers and experiment IDs
        cleaned = hypothesis
        cleaned = re.sub(r'\[TRANSFER[^\]]*\]\s*', '', cleaned)
        cleaned = re.sub(r'\[NEW from[^\]]*\]\s*', '', cleaned)
        cleaned = re.sub(r'^exp_\w+[\s:]', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if cleaned and len(cleaned) > 10:
            fragments.add(cleaned)

    if not fragments:
        return domain.replace("_", " ") + ", " + domain.replace("_", " ") + " research"

    # Convert to sorted list (stable ordering), take top N
    sorted_frags = sorted(fragments, key=lambda x: -len(x))
    selected = sorted_frags[:max_fragments]

    # Build description: domain topic name + unique experiment fragments
    description = domain.replace("_", " ") + ", " + ", ".join(selected)
    # Keep it under 2000 chars for embedding server limit
    if len(description) > 1900:
        description = description[:1900] + "..."

    return description


def auto_extend_descriptions(min_experiments: int = 5) -> list:
    """Detect domains in experiments missing from centroid cache and add them.

    Queries prometheus.db for domain labels that have >= min_experiments
    experiments but no embedding description yet, generates descriptions
    from the experiment hypotheses, saves them to the auto-descriptions file,
    and triggers a cache recompute.

    Returns: list of (domain, experiment_count, description_preview) tuples
    for newly added domains.
    """
    import sqlite3

    hermes = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    db_path = os.path.join(hermes, "prometheus.db")
    if not os.path.exists(db_path):
        print("prometheus.db not found, skipping auto-extend")
        return []

    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        c = conn.cursor()
        c.execute("""
            SELECT domain, COUNT(*) as cnt
            FROM experiments
            WHERE domain IS NOT NULL AND domain != '' AND domain != 'uncategorized'
            GROUP BY domain
            HAVING cnt >= ?
            ORDER BY cnt DESC
        """, (min_experiments,))
        domain_counts = dict(c.fetchall())
        conn.close()
    except Exception as e:
        print(f"Error querying domains: {e}")
        return []

    # Get current descriptions (hard-coded + existing auto)
    current = load_all_descriptions()

    # Find missing domains
    missing = {}
    for domain, cnt in domain_counts.items():
        if domain not in current:
            missing[domain] = cnt

    if not missing:
        return []

    # Generate descriptions for missing domains
    auto = load_auto_descriptions()
    added = []
    for domain, cnt in sorted(missing.items(), key=lambda x: -x[1]):
        desc = generate_domain_description(domain)
        auto[domain] = desc
        preview = desc[:60].replace("\n", " ")
        added.append((domain, cnt, preview))
        print(f"  Auto-generated centroid for '{domain}' ({cnt} exps): {preview}...")

    # Save
    save_auto_descriptions(auto)

    # Clear in-memory cache so next load recomputes centroids
    global _domain_embeddings
    _domain_embeddings = None

    # Recompute centroids for the new domains (embed them)
    domains_to_embed = [d for d, _, _ in added]
    descriptions_to_embed = [auto[d] for d in domains_to_embed]

    new_embs = get_embeddings_batch(descriptions_to_embed)
    # Load existing cache and add new centroids
    cache_path = os.path.expanduser("~/.hermes/domain_embeddings.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    else:
        cache = {}

    for domain, emb in zip(domains_to_embed, new_embs):
        if emb is not None:
            cache[domain] = emb

    # Save updated cache
    try:
        with open(cache_path, "w") as f:
            json.dump(cache, f)
        print(f"  Saved {len(new_embs)} new centroids to cache ({len(cache)} total)")
    except Exception as e:
        print(f"  Warning: could not save cache: {e}")

    return added


def detect_backend() -> str:
    """Detect available embedding backend. Returns 'qwen3' or 'none'.

    The ONNX bge-small-en path (:9151) is DISABLED as of 2026-07-05: it emits
    384-d vectors, incompatible with the 1024-d Qwen3 domain centroids. Always
    use the :9150 Qwen3-Embedding server so query and centroid dims match.
    """
    global ACTIVE_BACKEND
    if ACTIVE_BACKEND is not None:
        return ACTIVE_BACKEND

    import urllib.request

    # Qwen3 backend (:9150). ONNX (:9151, 384-d) intentionally NOT probed — it
    # would dimension-mismatch the 1024-d centroids.
    try:
        data = json.dumps({"input": "test", "model": EMBEDDING_MODEL}).encode("utf-8")
        req = urllib.request.Request(EMBEDDING_URL, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                ACTIVE_BACKEND = "qwen3"
                print(f"Using qwen3 backend (Qwen3-Embedding-0.6B, port 9150)")
                return "qwen3"
    except Exception:
        pass

    ACTIVE_BACKEND = "none"
    return "none"


def get_embedding(text: str) -> Optional[List[float]]:
    """Get embedding for a single text."""
    import urllib.request
    
    backend = detect_backend()
    
    if backend == "qwen3":
        url = EMBEDDING_URL
        model = EMBEDDING_MODEL
    else:
        return None
    
    try:
        data = json.dumps({
            "input": text,
            "model": model
        }).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result["data"][0]["embedding"]
    except Exception as e:
        return None


def get_embeddings_batch(texts: List[str]) -> List[Optional[List[float]]]:
    """Get embeddings for multiple texts (batch API)."""
    import urllib.request
    
    backend = detect_backend()
    
    if backend == "qwen3":
        url = EMBEDDING_URL
        model = EMBEDDING_MODEL
    else:
        return [None] * len(texts)
    
    try:
        data = json.dumps({
            "input": texts,
            "model": model
        }).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            # Return in order
            embeddings = [None] * len(texts)
            for item in result["data"]:
                embeddings[item["index"]] = item["embedding"]
            return embeddings
    except Exception as e:
        return [None] * len(texts)


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a = np.array(a)
    b = np.array(b)
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def load_domain_embeddings():
    """Load or compute domain embeddings."""
    global _domain_embeddings
    
    if _domain_embeddings is not None:
        return _domain_embeddings
    
    cache_path = os.path.expanduser("~/.hermes/domain_embeddings.json")
    
    # Try to load from cache
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                _domain_embeddings = json.load(f)
            return _domain_embeddings
        except Exception:
            _domain_embeddings = {}

    if not _domain_embeddings:
        # Compute embeddings for all domains using merged descriptions
        descriptions = load_all_descriptions()
        domains = list(descriptions.keys())
        domain_texts = [descriptions[d] for d in domains]
        print(f"Computing domain embeddings for {len(domains)} domains...")

        embeddings = get_embeddings_batch(domain_texts)

        _domain_embeddings = {}
        for domain, emb in zip(domains, embeddings):
            if emb is not None:
                _domain_embeddings[domain] = emb

        # Save cache
        try:
            with open(cache_path, "w") as f:
                json.dump(_domain_embeddings, f)
        except Exception:
            pass

        print(f"Computed embeddings for {len(_domain_embeddings)} domains")
    return _domain_embeddings


def classify_embedding(title: str, body: str = None) -> Tuple[str, float]:
    """
    Classify an experiment title using embeddings.
    
    Returns: (domain, confidence) tuple
    """
    # Pre-filter: skip test artifacts
    if re.match(r'^exp_(TEST|test|\d+)_', title):
        return ("general", 0.0)
    
    # Load domain embeddings
    domain_embs = load_domain_embeddings()
    if not domain_embs:
        return classify_regex(title, body)
    
    # Get title embedding
    title_emb = get_embedding(title)
    if title_emb is None:
        return classify_regex(title, body)
    
    # Compute similarities
    best_domain = "general"
    best_score = 0.0
    
    for domain, emb in domain_embs.items():
        sim = cosine_similarity(title_emb, emb)
        if sim > best_score:
            best_score = sim
            best_domain = domain
    
    # Apply threshold
    if best_score < SIMILARITY_THRESHOLD:
        return ("general", best_score)
    
    return (best_domain, best_score)


def classify_batch(titles: List[str]) -> List[Tuple[str, float]]:
    """
    Batch classify multiple titles.
    
    Returns: List of (domain, confidence) tuples
    """
    # Load domain embeddings
    domain_embs = load_domain_embeddings()
    if not domain_embs:
        return [classify_regex(t) for t in titles]
    
    # Get title embeddings in batch
    title_embs = get_embeddings_batch(titles)
    
    results = []
    for title, title_emb in zip(titles, title_embs):
        if title_emb is None:
            results.append(classify_regex(title))
            continue
        
        # Compute similarities
        best_domain = "general"
        best_score = 0.0
        
        for domain, emb in domain_embs.items():
            sim = cosine_similarity(title_emb, emb)
            if sim > best_score:
                best_score = sim
                best_domain = domain
        
        # Apply threshold
        if best_score < SIMILARITY_THRESHOLD:
            results.append(("general", best_score))
        else:
            results.append((best_domain, best_score))
    
    return results


# CLI interface
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Embedding-based domain classifier")
    parser.add_argument("text", nargs="?", help="Text to classify")
    parser.add_argument("--batch", help="File with one title per line")
    parser.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD, help="Similarity threshold")
    args = parser.parse_args()
    
    SIMILARITY_THRESHOLD = args.threshold
    
    if args.batch:
        with open(args.batch) as f:
            titles = [line.strip() for line in f if line.strip()]
        
        results = classify_batch(titles)
        for title, (domain, score) in zip(titles, results):
            print(f"{domain:30s} {score:.3f}  {title[:80]}")
    elif args.text:
        domain, score = classify_embedding(args.text)
        print(f"{domain:30s} {score:.3f}  {args.text[:80]}")
    else:
        # Interactive mode
        print("Enter titles to classify (Ctrl+C to exit):")
        try:
            while True:
                line = input("> ").strip()
                if line:
                    domain, score = classify_embedding(line)
                    print(f"  → {domain} (confidence: {score:.3f})")
        except (KeyboardInterrupt, EOFError):
            print()
