#!/usr/bin/env python3
"""
domain_classifier.py — Single source of truth for experiment domain classification.

All scripts that classify experiments by domain should import from here.
This prevents pattern drift between topology, inspector, and other tools.

Classification strategy: TITLE-FIRST PRIORITY
  1. Try to classify by title only (strong signal)
  2. If title has no match, fall back to body text (weak signal)
  3. Body-text matches are less reliable (mentions in passing, not research subject)

This prevents 97-98% false positives from body-text matching where domain
keywords appear in methodology descriptions, not as the research subject.

Usage:
    from domain_classifier import classify, DOMAIN_NAMES
    domain = classify("Can eddy current detect corrosion in concrete?")
    # Returns: 'physical_dynamics'

    domain = classify("exp_TEST: test hypothesis")
    # Returns: None (filtered as test artifact)
"""

import json
import os
import re
import numpy as np
import urllib.request
from datetime import datetime, timezone
from collections import Counter

# ── TITLE PATTERNS ──
# Strong signals: these patterns are reliable when matched in titles.
# Ordered by specificity (most specific first).

"""Domain Classifier.

Part of the Prometheus research infrastructure.
"""

TITLE_PATTERNS = [
    # Specific techniques
    (r'TF-IDF|tfidf', 'tfidf'),
    (r'PCA|SVD|subspace|\\bdimensionality\\b|\\bdimension.*reduc', 'dim_reduction'),
    (r'\\bdict\\b|fact.?reduction|fact.?set|fact.*generaliz', 'dict_methodology'),
    (r'dispatch|two.?stage.*pipeline|pipeline.*eval', 'dispatch_pipeline'),
    (r'eu.*ai.*act|basel.*iii|coppa|gdpr|regulat|compliance|privacy', 'regulatory'),
    (r'accuracy|f1.*score|precision|recall|benchmark', 'metrics_benchmarking'),
    (r'optimal|efficiency|cost.*reduc|minim.*set|sweet.?spot', 'optimization'),
    (r'temperature|thermal|heat.*surface|surface.*temper', 'temperature_analysis'),
    # Physical dynamics — NDT / inspection / corrosion / materials / natural systems
    (r'eddy.current|ultrasonic|thermograph|impedance.*spectro|'
     r'fluoresc.*imag|terahertz|neutron.*radiograph|'
     r'capacitance.*tomograph|reflectometr|bragg.*grating|'
     r'magneto.?optical|schlieren|rayleigh.*backscatter|'
     r'cavity.*perturbation|lock.?in.*therm|'
     r'time.?of.?flight.*mass|electromagnetic.*acoustic|'
     r'fiber.*optic.*detect|fiber.*optic.*distributed|'
     r'microwave.*ndt|microwave.*reflect|microwave.*cavity|vibromet|'
     r'corrosion|fatigue.*crack|crack.*propagat|stress.*corros|delaminat|disbond|erosion|'
     r'reinforced.*concrete|honeycomb|ceramic.*matrix|'
     r'maillard|browning.*reaction|viscosity|tensile|crystalline|'
     r'acoustic.*propert|acoustic.*propagat|structural.*integrity|'
     r'relaxation.*rate|stretched.*exponential|'
     r'coral.*growth|ocean.*acidif|leather.*tanning|collagen.*fiber|'
     r'paper.*grain|paper.*fiber|garden.*plot|rope.*wet|flytrap|'
     r'gemstone.*dispers|coral.*reef|biosensing|'
     r'powder.*recycl|slm.*part|ion.*mass.*spectro|'
     r'slug.*flow|wall.*thin|food.*product|'
     r'composite.*boat|masonry|construction.*material|'
     r'aerospace.*composite|process.*piping|ferromagnetic|'
     r'transparent.*polymer|thin.*film.*interconnect|'
     r'bridge.*cable|wind.*turbine.*blade|concrete.*dam|'
     r'flavor.*change|sound.*change|creaking|varnish|conch|'
     r'seasoned.*cast.*iron|balsamic|vinegar|pepper.*smell|'
     r'bamboo.*scaffold|spider.*silk|ocean.*waves.*seabed|'
     r'parmesan|cheese.*crunch|wood.*ship|horse.*hooves|'
     r'piano.*soundboard|concert.*hall.*tapestry|'
     r'handmade.*glass.*bubbles|bread.*crust.*steam|'
     r'coffee.*altitude|violin.*warm|chocolate.*conch',
     'physical_dynamics'),
    # Broad domains
    (r'injection|prompt.?inject', 'injection_detection'),
    (r'calibrat', 'calibration'),
    (r'adversar|evasion|bypass|exploit|'
     r'authority.*trust|authority.*attack|authority.*defer|authority.*suscept',
     'adversarial'),
    (r'ensemble', 'ensemble'),
    (r'jailbreak', 'jailbreak'),
    (r'attention', 'attention'),
    (r'bias|fairness', 'bias'),
    (r'cost|pricing', 'cost_analysis'),
    (r'vocabulary|lexical|synonym', 'lexical'),
    (r'safety|harm', 'safety'),
    (r'LLM|language.?model', 'llm_general'),
    (r'meta|self|curiosity|synthesis', 'meta_research'),
    # System infrastructure — Hermes internals
    (r'sqlite|pragma|kanban\\.db|embedding.?server|health.?signal|'
     r'gateway.?stall|state\\.db|queue.?curator|experiment.?rag|'
     r'nomic.?embed|gpu.?utiliz|'
     r'hermes.*(?:cron|script|session|database|agent)',
     'system_infrastructure'),
    # ── Natural science / biology / medicine ──
    # Added June 2026: these were missing, causing 88% natural science misclassification
    (r'cytokine|immunolog|pathogen|antibod|autoimmun', 'medicine', 3),
    (r'cardio|cardiac|heart.*failure|arrhythm|ECG|echocardi', 'cardiology', 3),
    (r'protein.*fold|protein.*struct|protein.*dynam|protein.*interact|amyloid', 'biophysics', 3),
    (r'mycel|fungi|fungal|mycorrhiz|spore', 'mycology', 3),
    (r'seed.*dorm|germinat|fire.*ecolog|fire.*regime|pyrogen', 'ecology', 3),
    (r'hyphal|mycelial|basidio', 'mycology', 3),
    (r'ventral.*scale|colubrid|herpetolog|reptile', 'biology', 3),
    (r'warbler.*flock|magnification.*bird|avian|bird.*identif', 'biology', 3),
    (r'brain|neuron|synaps|cognitive|neural.*circuit|hippocamp', 'neuroscience', 3),
    (r'stream.*power|channel.*morpho|dam.*removal|fluvial|erosion.*rate', 'geomorphology', 3),
    (r'seismic|earthquake|fault.*mechan|P-wave|S-wave', 'seismology', 3),
    (r'glacier|ice.*sheet|permafrost|glacial', 'geomorphology', 3),
    (r'soil.*carbon|soil.*organic|soil.*microb|soil.*respirat', 'soil_science', 3),
    (r'volcanic|magma|eruption|lava', 'volcanology', 3),
    (r'porous.*silicon|thermal.*conduct|phonon.*scatter|nanoporous', 'semiconductor_physics', 3),
    (r'HVAC|building.*envelope|air.*tight|infiltration.*rate|R-value', 'engineering', 3),
    (r'biolog|organism|species|evolution|ecology|physiology|genetic|arthropod|insect|crustacean', 'biology', 2),
    (r'ecosystem|biodiversity|conservation|habitat|population.*dynamic', 'ecology', 2),
    (r'medicine|clinical|patient|disease|treatment|drug|therapy|dosage', 'medicine', 2),
    (r'physics|thermodynamic|quantum|fluid.*dynamic|wave|optics|acoustic|sound.*propagat|ocean.*shallow', 'physics', 2),
    (r'chemistry|chemical.*reaction|molecular|organic.*chem|distillat|tannage|attenuat', 'chemistry', 2),
    (r'geolog|rock|mineral|sediment|stratigraph', 'geophysics', 2),
    (r'climate|atmospher|meteorolog|weather|ocean.*current', 'atmospheric_science', 2),
    (r'material|alloy|polymer|ceramic|composite|crystal|fracture|fatigue', 'materials_science', 2),
    # Cross-domain prediction — intentionally bridges multiple domains
    # Kept LAST as it's the most generic and should lose ties to specific domains
    (r'predict|prediction', 'cross_domain_prediction', 1),
]

# ── BODY PATTERNS (fallback only) ──
# Weaker signals: only used when title has no match.
# Same patterns as title, but applied to body text.
# Body matches are less reliable — domain keywords appear in
# methodology descriptions, not as the research subject.
BODY_PATTERNS = TITLE_PATTERNS  # Same patterns, just applied to body text

# All valid domain names (for reference/validation)
DOMAIN_NAMES = sorted(set(entry[1] for entry in TITLE_PATTERNS))

# ── DOMAIN ROLES ──
# Maps each domain to its role in the research topology.
DOMAIN_ROLES = {
    "foundational": ["lexical", "dict_methodology", "dim_reduction", "cost_analysis"],
    "deployment": ["injection_detection", "adversarial", "jailbreak", "attention", "bias", "safety", "llm_general", "ensemble"],
    "synthesis": ["cross_domain_prediction", "meta_research", "metrics_benchmarking", "optimization", "calibration", "tfidf"],
    "infrastructure": ["system_infrastructure", "dispatch_pipeline"],
    "physical": ["physical_dynamics", "temperature_analysis", "regulatory"],
    "general": ["general"],
}

# ── FOUNDATIONAL REFERENCES ──
# Maps foundational domains to REGEX PATTERNS that reference their techniques.
# Used by route_with_instrumentation to detect cross-domain references.
# Patterns use regex, not substring matching — word boundaries prevent false positives.
FOUNDATIONAL_REFERENCES = {
    "lexical": [
        r'\bvocabulary\b', r'\blexical\b', r'\btoken\b.*(?:pattern|frequen|overlap|entrop)',
        r'\bword\b.*(?:frequen|overlap|count|entrop)', r'\bn.?gram',
        r'tfidf', r'tf-idf', r'bag.of.words',
    ],
    "dict_methodology": [
        r'\bdict\b', r'fact.?reduction', r'fact.?set', r'fact.?minimiz',
    ],
    "tfidf": [
        r'tfidf', r'tf-idf', r'\bvectorizer\b', r'bag.of.words', r'term.frequen',
    ],
    "dim_reduction": [
        r'\bpca\b', r'\bsvd\b', r'\bsubspace\b', r'\bdimensionality\b',
        r'\bembedding\b.*(?:dimension|reduc|project|distance|comput)',
        r'\bprojection\b.*(?:method|technique|reduc)',
    ],
    "calibration": [
        r'\bcalibrat', r'\bisotonic\b', r'\bplatt\b', r'temperature.scal',
    ],
    "cost_analysis": [
        r'\binference.cost\b', r'compute.cost', r'token.cost',
        r'\bcost.*(?:reduc|optim|model|analy)', r'\bpric.*(?:model|analy)',
        r'\blatency.*(?:budget|target|cost)',
    ],
}


def _score_matches(text):
    """Score all regex matches against text. Returns {domain: score}.
    
    Replaces first-match-wins with weighted scoring.
    Each pattern match adds its weight to the domain's total score.
    Weight is explicit in TITLE_PATTERNS (3rd element) or defaults to 1.
    """
    text_lower = text.lower()
    scores = {}
    
    for entry in TITLE_PATTERNS:
        pattern, d = entry[0], entry[1]
        weight = entry[2] if len(entry) > 2 else 1
        if re.search(pattern, text_lower, re.IGNORECASE):
            scores[d] = scores.get(d, 0) + weight
    
    return scores


def classify(title, body=None):
    """Classify an experiment into a research domain.

    Classification strategy: WEIGHTED SCORING (replaces first-match-wins)
      1. Score ALL regex matches in title (strong signal)
      2. Score ALL regex matches in body (weak signal, lower weight)
      3. Return highest-scoring domain
    
    Returns domain name string, or None for test/debug artifacts.
    Returns 'general' if no pattern matches.
    """
    title_lower = title.lower()

    # Filter out test/debug artifacts
    if re.match(r'exp[_\-]test', title_lower):
        return None

    # Score title matches (full weight)
    scores = _score_matches(title)

    # Score body matches (half weight — body keywords are weaker signal)
    if body:
        body_scores = _score_matches(body)
        for d, s in body_scores.items():
            scores[d] = scores.get(d, 0) + s * 0.5

    if not scores:
        return 'general'

    # Return highest-scoring domain
    return max(scores, key=scores.get)


def classify_multi(title, body=None):
    """Multi-label classification with full scoring.
    
    Returns dict:
        primary_domain: str — top-scoring domain
        domain_scores: dict — all domains with scores, sorted descending
    """
    title_lower = title.lower()

    if re.match(r'exp[_\-]test', title_lower):
        return {"primary_domain": None, "domain_scores": {}}

    scores = _score_matches(title)

    if body:
        body_scores = _score_matches(body)
        for d, s in body_scores.items():
            scores[d] = scores.get(d, 0) + s * 0.5

    if not scores:
        return {"primary_domain": "general", "domain_scores": {"general": 1.0}}

    # Normalize scores to [0, 1]
    max_score = max(scores.values())
    normalized = {d: s / max_score for d, s in scores.items()}
    ranked = sorted(normalized.items(), key=lambda x: -x[1])

    return {
        "primary_domain": ranked[0][0],
        "domain_scores": dict(ranked)
    }


def route_with_instrumentation(source_domain, target_domain, body):
    """Check if body references any foundational domain's techniques.

    Returns a list of (foundational_domain, keyword_matched, confidence) tuples.
    Returns empty list if no foundational references found.
    This is the reactive routing with instrumentation.
    Uses regex patterns, not substring matching.
    """
    if not body:
        return []

    body_lower = body.lower()
    results = []
    seen_domains = set()

    for domain, patterns in FOUNDATIONAL_REFERENCES.items():
        for pattern in patterns:
            if re.search(pattern, body_lower, re.IGNORECASE):
                if domain not in seen_domains:
                    results.append((domain, pattern, 0.8))
                    seen_domains.add(domain)
                break  # One match per domain is sufficient

    return results


def classify_with_transfer(title, body):
    """Classify a task's source domain and transfer target.

    Returns (source_domain, target_domain, additional_targets) tuple.
    target_domain is None if no [TRANSFER] tag.
    additional_targets is a list of (domain, keyword, confidence) tuples
    from reactive routing to foundational domains.
    """
    source = classify(title, body)
    if source is None:
        return None, None, []
    if '[TRANSFER]' not in (body or ''):
        return source, None, []
    transfer_section = body.split('[TRANSFER]')[1] if '[TRANSFER]' in body else ''
    # Transfer targets are classified from body text only (no title context)
    target = classify(transfer_section, transfer_section) if transfer_section else 'general'
    if target == 'general' or target == source:
        # Weighted fan-out: instead of dumping into transfer_learning,
        # classify by embedding similarity against domain centroids
        target = _fallback_classify(transfer_section or title, source)

    # Reactive routing: check body for foundational domain references
    # This creates additional edges from deployment domains to foundational domains
    additional_targets = route_with_instrumentation(source, target, body)

    # Log the routing decision for topology visualization
    log_routing_decision(source, target, additional_targets, (body or '')[:500])

    return source, target, additional_targets


# ── FALLBACK CLASSIFIER (weighted fan-out) ──
# When regex classification fails, use embedding similarity against
# precomputed domain centroids. Replaces the old transfer_learning catch-all.

_CENTROIDS_CACHE = None
_CENTROID_SIMILARITY_THRESHOLD = 0.25
_FALLBACK_TOP_K = 3

def _load_centroids():
    """Load precomputed domain centroid embeddings."""
    global _CENTROIDS_CACHE
    if _CENTROIDS_CACHE is not None:
        return _CENTROIDS_CACHE
    centroid_path = os.path.expanduser("~/.hermes/domain_embeddings.json")
    try:
        with open(centroid_path) as f:
            data = json.load(f)
        _CENTROIDS_CACHE = {d: np.array(e) for d, e in data.items()}
    except Exception:
        _CENTROIDS_CACHE = {}
    return _CENTROIDS_CACHE

def _embed_text(text):
    """Embed text using the local embedding model (qwen3-embedding-0.6b)."""
    url = "http://localhost:9150/v1/embeddings"
    data = json.dumps({"input": [text[:512]], "model": "qwen3-embedding-0.6b"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return np.array(resp["data"][0]["embedding"])
    except Exception:
        return None

def _fallback_classify(text, exclude_domain=None):
    """Classify by cosine similarity against domain centroids.
    Returns the best-matching domain, or 'general' if nothing matches."""
    centroids = _load_centroids()
    if not centroids:
        return 'general'
    emb = _embed_text(text)
    if emb is None:
        return 'general'
    emb_norm = np.linalg.norm(emb)
    if emb_norm == 0:
        return 'general'

    exclude = {exclude_domain, 'general', 'transfer_learning'}
    best_domain = 'general'
    best_score = 0.0
    for domain, centroid in centroids.items():
        if domain in exclude:
            continue
        sim = float(np.dot(emb, centroid)) / (emb_norm * np.linalg.norm(centroid))
        if sim > best_score:
            best_score = sim
            best_domain = domain

    if best_score < _CENTROID_SIMILARITY_THRESHOLD:
        return 'general'
    return best_domain


# ── PERSISTENT INSTRUMENTATION LOGGING ──
LOG_DIR = os.path.expanduser("~/.hermes/classifier")
LOG_PATH = os.path.join(LOG_DIR, "routing_log.jsonl")


def log_routing_decision(source, target, additional_targets, body_snippet):
    """Append a routing decision to the persistent JSONL log.

    Each line: {"timestamp": ..., "source": ..., "target": ...,
                "additional": [...], "body_snippet": "..."}

    The directory and file are created on first call (append-only).
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "target": target,
        "additional": [
            {"domain": d, "keyword": k, "confidence": c}
            for d, k, c in (additional_targets or [])
        ],
        "body_snippet": (body_snippet or "")[:500],
    }

    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


# ── ANOMALY DETECTION ──

# Stopwords ignored when extracting key terms for overlap comparison.
_STOP = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "this", "that",
    "these", "those", "it", "its", "we", "our", "they", "their", "not",
    "no", "if", "as", "so", "than", "then", "also", "into", "over",
    "about", "between", "through", "during", "before", "after", "above",
    "below", "up", "down", "out", "off", "again", "further", "once",
    "exp", "test", "hypothesis",
})

# Words ≤ 2 characters are ignored (too generic).
_MIN_WORD_LEN = 3


def _extract_terms(text):
    """Return set of meaningful lowercase terms from text."""
    if not text:
        return set()
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) >= _MIN_WORD_LEN and w not in _STOP}


def detect_anomalies(source_domain, finding_text, body,
                     _title_cache=None):
    """Check whether a finding is novel relative to the domain's knowledge.

    Uses the domain's existing task titles (from kanban.db) as the baseline.
    Computes term-overlap between the finding and the baseline title corpus.

    Returns dict:
        is_anomaly   – True if overlap < 20 % (novel finding)
        reason       – human-readable explanation
        baseline_size – number of baseline titles used
        similarity_score – fraction of finding terms found in baseline [0..1]
    """
    # ── Load baseline titles for this domain from kanban.db ──
    db_path = os.path.expanduser("~/.hermes/kanban.db")
    baseline_titles = []
    if os.path.exists(db_path):
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
            cur = conn.cursor()
            cur.execute(
                "SELECT title, body FROM tasks "
                "WHERE title LIKE 'exp_%' AND body IS NOT NULL"
            )
            for t, b in cur.fetchall():
                d = classify(t, b)
                if d == source_domain:
                    baseline_titles.append(t)
            conn.close()
        except Exception:
            pass  # graceful degradation — treat as empty baseline

    if not baseline_titles:
        return {
            "is_anomaly": False,
            "reason": f"No baseline titles found for domain '{source_domain}'; "
                     "cannot determine anomaly status",
            "baseline_size": 0,
            "similarity_score": 0.0,
        }

    # ── Build baseline term set ──
    baseline_text = " ".join(baseline_titles)
    baseline_terms = _extract_terms(baseline_text)

    # ── Extract terms from the finding ──
    finding_combined = f"{finding_text} {body or ''}"
    finding_terms = _extract_terms(finding_combined)

    if not finding_terms:
        return {
            "is_anomaly": False,
            "reason": "No meaningful terms in finding to compare",
            "baseline_size": len(baseline_titles),
            "similarity_score": 1.0,
        }

    # ── Compute overlap ──
    overlap = finding_terms & baseline_terms
    similarity_score = len(overlap) / len(finding_terms) if finding_terms else 1.0
    is_anomaly = similarity_score < 0.20

    if is_anomaly:
        novel = finding_terms - baseline_terms
        sample = sorted(novel)[:8]
        reason = (
            f"Novel finding for '{source_domain}': "
            f"{similarity_score:.0%} term overlap with baseline. "
            f"Unseen terms include: {', '.join(sample)}"
        )
    else:
        reason = (
            f"Consistent with existing '{source_domain}' knowledge: "
            f"{similarity_score:.0%} term overlap with baseline"
        )

    return {
        "is_anomaly": is_anomaly,
        "reason": reason,
        "baseline_size": len(baseline_titles),
        "similarity_score": round(similarity_score, 4),
    }


# ── AUTO-GENERATED PATTERNS FROM DB DOMAINS ──
# Every domain that exists in the experiments table gets an auto-generated
# regex pattern built from its own name. This eliminates the coverage gap
# where domains exist but have no matching pattern, causing them to fall
# through to whatever ML-adjacent pattern matches first.
#
# These are weight 3 — matching the specific hand-crafted patterns.
# Auto-generated patterns are inherently more specific (they use the
# exact domain name words), so they should win against generic patterns
# like 'biology' when the match is precise.

def _domain_to_pattern(domain):
    """Convert a domain name like 'blockchain_consensus' to a regex pattern
    like r'blockchain.*consensus|consensus.*blockchain'."""
    words = domain.replace('_', ' ').split()
    # Keep words >= 2 chars but filter noise (single letters, common short words)
    _noise = {'of', 'in', 'to', 'by', 'at', 'on', 'an', 'or', 'is', 'be', 'as', 'no', 'if', 'so'}
    words = [w for w in words if len(w) >= 2 and w not in _noise]
    if not words:
        return None
    if len(words) == 1:
        return r'\b' + words[0] + r'\b'  # word boundary prevents substring false matches
    # Multi-word: match both orders
    forward = '.*'.join(words)
    backward = '.*'.join(reversed(words))
    return f'{forward}|{backward}'


def _load_db_domains():
    """Return set of all unique domains from the experiments table."""
    try:
        import sqlite3
        db = os.path.expanduser("~/.hermes/prometheus.db")
        if not os.path.exists(db):
            return set()
        conn = sqlite3.connect(db, timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        rows = conn.execute(
            "SELECT DISTINCT domain FROM experiments WHERE domain IS NOT NULL AND domain != ''"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _augment_patterns():
    """Add auto-generated patterns for DB domains that lack hand-crafted ones."""
    existing = {entry[1] for entry in TITLE_PATTERNS}
    db_domains = _load_db_domains()
    missing = db_domains - existing
    new = []
    for d in sorted(missing):
        pat = _domain_to_pattern(d)
        if pat:
            new.append((pat, d, 3))  # weight 3 — matches specific hand-crafted
    return new


# Augment at import time — runs once when this module is first loaded.
_AUTO_PATTERNS = _augment_patterns()
if _AUTO_PATTERNS:
    TITLE_PATTERNS = list(TITLE_PATTERNS) + _AUTO_PATTERNS
    BODY_PATTERNS = TITLE_PATTERNS
    DOMAIN_NAMES = sorted(set(entry[1] for entry in TITLE_PATTERNS))
