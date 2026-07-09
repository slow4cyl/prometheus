#!/usr/bin/env python3
"""
Curiosity Priority Scorer — determines which queue items the Director should
prioritize. Reads self_state.json and completed experiments, computes a 0-100
priority score per queue item.

RESCORING (exp_72526550 / exp_72526308 batch):
  Previous version had novelty and diversity positively weighted, but data
  shows novelty r=-0.24 with success and diversity r=-0.24. Source quality
  (r=+0.18) is the ONLY component that predicts success. The formula was
  actively inverting its own signal.

  New formula:
    source_quality:      0-50 (dominant, only positive predictor)
    diminishing:         0-25 (inverted — LOW diminishing returns = HIGH score)
    novelty:             0-10 (rewards novel/under-represented threads)
    diversity:           0-10 (rewards under-represented threads in queue)
    transfer_bonus:      +10 (cross-domain [TRANSFER] items, smaller than before)
    total cap:           100

  Removed: "other" thread bonus (+35), per-thread score adjustments.
  These were patches for the wrong problem.

CALIBRATION FIX (June 6 2026, exp_72526522):
  The scorer was penalizing "other" (uncategorized) items to score ~23,
  but "other" items have the HIGHEST support rate (75%). Root cause:
  compute_diversity_bonus returned 0 for thread_count=0, dragging down
  total scores for items that don't match any thread keyword. Fixed to
  return neutral baseline (5) for unexplored threads.

Dedup: Intra-queue duplicates are detected via is_already_in_queue() — items
with >0.7 word overlap against earlier queue entries get score=0 and
thread="duplicate". This prevents the Director from creating tasks for
duplicate queue items the synthesis added across cycles.

Queue Diversification (exp_72526491 fix): The curiosity queue is a local
attractor — it feeds back toward recently explored topics, creating depth
loops. The --diversify flag injects orthogonal questions when one thread
dominates >35% of the queue. This implements the 80/20 hybrid: 80% depth
(normal scored queue), 20% breadth (forced orthogonal exploration).
Freezing the queue increased diversity by 0.474 bits (Shannon entropy
2.633 → 3.107). This function achieves similar results without freezing.

Usage:
    python3 curiosity_scorer.py                   # Score all active queue items
    python3 curiosity_scorer.py --top 10          # Show top 10 only
    python3 curiosity_scorer.py --thread tfidf    # Filter by thread
    python3 curiosity_scorer.py --json            # JSON output for Director
    python3 curiosity_scorer.py --report          # Full report with recommendations
    python3 curiosity_scorer.py --diversify       # Inject orthogonal questions if queue is homogeneous
"""

import json
import re
import os
import sys
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone

_CLAIM_CACHE = {"statuses": None, "timestamp": 0}
_CLAIM_CACHE_TTL = 300  # 5 minutes

def load_claim_statuses():
    """Load claim statuses from the claim registry.
    
    Returns dict mapping claim_text_substring -> status.
    Used to zero out scoring components that have been downgraded
    by the epistemic audit (June 12 2026).
    
    Example: if "novelty scoring" claim is downgraded, novelty component
    is zeroed out in score_item().
    """
    import time
    now = time.time()
    if (_CLAIM_CACHE["statuses"] is not None and
            now - _CLAIM_CACHE["timestamp"] < _CLAIM_CACHE_TTL):
        return _CLAIM_CACHE["statuses"]

    statuses = {}
    try:
        import sqlite3 as _sql
        _db_path = os.path.expanduser("~/.hermes/prometheus.db")
        _conn = _sql.connect(_db_path, timeout=5)
        _conn.execute("PRAGMA busy_timeout=3000")
        rows = _conn.execute(
            "SELECT claim_text, status, confidence FROM architectural_claims"
        ).fetchall()
        _conn.close()
        for claim_text, status, confidence in rows:
            # Use key phrases to match claims to scoring components
            lower = claim_text.lower()
            if "novelty" in lower and "scor" in lower:
                statuses["novelty"] = {"status": status, "confidence": confidence}
            elif "diversity" in lower and "scor" in lower:
                statuses["diversity"] = {"status": status, "confidence": confidence}
            elif "transfer" in lower and ("track" in lower or "table" in lower):
                statuses["transfer_tracking"] = {"status": status, "confidence": confidence}
            elif "domain" in lower and "taxonomy" in lower:
                statuses["domain_taxonomy"] = {"status": status, "confidence": confidence}
            elif "source quality" in lower:
                statuses["source_quality"] = {"status": status, "confidence": confidence}
            elif "novelty injection" in lower or "cross-domain novelty" in lower:
                statuses["novelty_injection"] = {"status": status, "confidence": confidence}
            elif "build" in lower and "pipeline" in lower:
                statuses["build"] = {"status": status, "confidence": confidence}
    except Exception:
        pass  # registry unavailable — don't affect scoring

    _CLAIM_CACHE["statuses"] = statuses
    _CLAIM_CACHE["timestamp"] = now
    return statuses

"""CLI tool: Curiosity Scorer.

Usage: python3 curiosity_scorer.py [options]
"""


SELF_STATE_PATH = os.path.expanduser("~/.hermes/self_state.json")
RECENT_N = 20  # Number of recent experiments to compare against

# Experiment index cache — avoids rebuilding 14K+ experiment index every call
_EXP_INDEX_CACHE = {"index": None, "timestamp": 0}
_EXP_INDEX_TTL = 300  # 5 minutes

# ── Replication-state snapshot freshness guard ──────────────────────────────
# curiosity_scorer reads domain break-informativeness / disagreement scores from
# ~/.hermes/replication_state.json, a snapshot written by replication_tracker.py
# (every 15 min). If that refresher dies, the file silently goes stale and the
# scorer keeps reading old domain scores with no signal that anything is wrong
# (the old code's `except: pass` even tolerated a MISSING file silently). The
# snapshot stamps `last_updated` (unix int), so we can detect staleness and fall
# back to the live DB column. Threshold = 4 missed 15-min cycles.
REPL_STATE_PATH = os.path.expanduser("~/.hermes/replication_state.json")
REPL_STATE_MAX_AGE_S = 3600  # 1h; replication_tracker runs every 15m
_REPL_STATE_CACHE = {"data": None, "stale": None, "loaded_at": 0, "live_breaks": None}
_REPL_STATE_TTL = 60  # re-stat the file at most once a minute
_REPL_STALE_WARNED = False  # warn-once per process so we don't spam stderr


def load_replication_state():
    """Return (repl_dict, is_stale). repl_dict is the parsed snapshot (or {} if
    missing/unreadable). is_stale is True when the snapshot is absent or its
    last_updated is older than REPL_STATE_MAX_AGE_S. Cached for _REPL_STATE_TTL
    so per-item scoring doesn't re-stat the file thousands of times per run.
    Emits a one-time stderr warning when stale (visible in the cron log)."""
    import time
    global _REPL_STALE_WARNED
    now = time.time()
    if _REPL_STATE_CACHE["data"] is not None and (now - _REPL_STATE_CACHE["loaded_at"]) < _REPL_STATE_TTL:
        return _REPL_STATE_CACHE["data"], _REPL_STATE_CACHE["stale"]
    data, stale = {}, True
    try:
        if os.path.exists(REPL_STATE_PATH):
            with open(REPL_STATE_PATH) as f:
                data = json.load(f)
            age = now - float(data.get("last_updated", 0) or 0)
            stale = age > REPL_STATE_MAX_AGE_S
        else:
            age = None
    except Exception:
        data, stale = {}, True
    if stale and not _REPL_STALE_WARNED:
        try:
            sys.stderr.write(
                "[curiosity_scorer] WARNING: replication_state.json is stale or "
                "missing (replication_tracker may be down); break/disagreement "
                "scores falling back to the live DB column.\n")
        except Exception:
            pass
        _REPL_STALE_WARNED = True
    _REPL_STATE_CACHE.update(data=data, stale=stale, loaded_at=now)
    if not stale:
        _REPL_STATE_CACHE["live_breaks"] = None  # snapshot is good; drop any fallback cache
    return data, stale


def live_domain_break_score(source_domain):
    """Fallback: compute one domain's break-informativeness directly from the
    live replication_results column when the snapshot is stale. Mirrors
    replication_tracker.save_state()'s aggregation (avg over break_informativeness>0,
    non-pending rows). Cached as a whole dict per process so a stale run does ONE
    query, not one-per-item. Returns {'avg_informativeness': float, 'break_count': int}
    or None if the domain has no break rows."""
    if not source_domain:
        return None
    cache = _REPL_STATE_CACHE.get("live_breaks")
    if cache is None:
        cache = {}
        try:
            import sqlite3
            _db = os.path.expanduser("~/.hermes/prometheus.db")
            _conn = sqlite3.connect(f"file:{_db}?mode=ro", uri=True, timeout=5)
            _conn.execute("PRAGMA busy_timeout=3000")
            for dom, avg_bi, cnt in _conn.execute(
                "SELECT original_domain, AVG(break_informativeness), COUNT(*) "
                "FROM replication_results "
                "WHERE replication_status != 'pending' AND break_informativeness > 0 "
                "GROUP BY original_domain"):
                cache[dom or "unknown"] = {
                    "avg_informativeness": round(avg_bi or 0, 1),
                    "break_count": int(cnt or 0),
                }
            _conn.close()
        except Exception:
            cache = {}
        _REPL_STATE_CACHE["live_breaks"] = cache
    return cache.get(source_domain)


def live_domain_disagreement(source_domain):
    """Fallback for the disagreement-rate penalty when the snapshot is stale.
    Computes one domain's {disagreement_rate, total} live from replication_results.
    Cached whole-dict per process. Returns None if the domain has no rows."""
    if not source_domain:
        return None
    cache = _REPL_STATE_CACHE.get("live_disagree")
    if cache is None:
        cache = {}
        try:
            import sqlite3
            _db = os.path.expanduser("~/.hermes/prometheus.db")
            _conn = sqlite3.connect(f"file:{_db}?mode=ro", uri=True, timeout=5)
            _conn.execute("PRAGMA busy_timeout=3000")
            for dom, total, dis in _conn.execute(
                "SELECT original_domain, COUNT(*), "
                "       SUM(CASE WHEN replication_status='disagreed' THEN 1 ELSE 0 END) "
                "FROM replication_results WHERE replication_status != 'pending' "
                "GROUP BY original_domain"):
                t = int(total or 0)
                cache[dom or "unknown"] = {
                    "total": t,
                    "disagreement_rate": round((dis or 0) / t, 3) if t else 0,
                }
            _conn.close()
        except Exception:
            cache = {}
        _REPL_STATE_CACHE["live_disagree"] = cache
    return cache.get(source_domain)


def live_disagreed_experiment_ids():
    """Fallback for the specific-disagreed-experiment penalty when the snapshot
    is stale. Returns the live set of original_experiment_id with status
    'disagreed'. Cached per process."""
    cache = _REPL_STATE_CACHE.get("live_disagreed_ids")
    if cache is None:
        cache = set()
        try:
            import sqlite3
            _db = os.path.expanduser("~/.hermes/prometheus.db")
            _conn = sqlite3.connect(f"file:{_db}?mode=ro", uri=True, timeout=5)
            _conn.execute("PRAGMA busy_timeout=3000")
            cache = {r[0] for r in _conn.execute(
                "SELECT original_experiment_id FROM replication_results "
                "WHERE replication_status='disagreed'")}
            _conn.close()
        except Exception:
            cache = set()
        _REPL_STATE_CACHE["live_disagreed_ids"] = cache
    return cache

# Thread classification keywords
THREAD_KEYWORDS = {
    "tfidf": ["tf-idf", "tfidf", "signal concentration", "feature importance"],
    "lr_detection": ["logistic regression", " lr ", " lr\n", "lr-"],
    "psychology": ["psychiatric", "psychology", "mental health", "depression", "anxiety", "ptsd", "suicid", "therapy", "clinical note", "diagnosis", "symptom", "screening", "treatment outcome", "medication"],
    "cross_lingual": ["cross-lingual", "multilingual", "code-mixed", "code-switching", "language switch", "cross-lingual", "ilingual", "french", "spanish", "chinese", "arabic", "translation"],
    "injection": ["injection", "injected", "inject", "toxicity", "hallucination", "content moderation", "false positive", "refusal", "jailbreak"],
    "calibration": ["calibration", "isotonic", "platt", "temperature scaling", "confidence", "hedging", "inverted-u", "confidence pattern", "confidence threshold"],
    "attack": ["attack", "adversarial", "perturbation", "repetition attack", "spacing", "safety training", "attention pattern", "defense"],
    "hardware": ["hardware", "edge device", "rpi", "raspberry", "fpga", "simd", "arm neon", "jetson", "federated learning", "distributed"],
    "domain": ["multi-domain", "cross-domain", "per-domain", "300-domain", "100+ domain", "universal", "generalize", "generalization"],
    "embedding": ["embedding", "vector representation", "word2vec", "feature granularity", "feature representation"],
    "workspace": ["workspace", "cleanup", "ttl", "archival", "garbage collection"],
    "cross_pollination": ["[transfer]", "[TRANSFER]"],
    "transfer": ["transfer learning", "few-shot", "data augmentation"],
    "consistency": ["consistency", "variance-based", "response variance"],
    "matrix_lr": ["matrix lr", "matrix multiply", "matrix multiply"],
    "ensemble": ["ensemble", "contradiction probing", "defense-in-depth", "subadditivity", "modalities", "detector"],
    "metacognition": ["cognitive load", "experiment scheduling", "curiosity queue", "research trajectory",
                      "hypothesis generation", "confirmation bias", "diminishing returns", "meta-predictor",
                      "agent's own", "agent learn", "agent construct", "agent detect", "agent predict",
                      "agent measure", "agent exhibit", "research efficiency", "queue.*converged",
                      "local optimum", "causal graph", "experiment fatigue", "batch size.*parallel",
                      "cost-per-discovery", "optimal number of.*worker", "calibration error.*curiosity",
                      "information density.*meta-analysis", "cold-start", "pareto frontier.*experiment"],
    # Core ML/systems threads (added June 7 2026 — were missing, causing 15% "other")
    "machine_learning": ["quantization", "transformer", "llm", "hyperparameter", "cnn", "dimensional fragment", "capacity-sufficient", "representation", "deep feature", "statistical feature",
                         "attention", "neural network", "deep learning", "fine-tuning",
                         "pre-training", "loss landscape", "gradient", "optimizer",
                         "batch size", "learning rate", "overfitting", "generalization",
                         "embedding", "representation learning", "feature extraction",
                         "classification", "regression", "detection model"],
    "complex_systems": ["topology", "scale-free", "cascade", "network resilience",
                        "load balanc", "failure cascade", "resilience", "robustness",
                        "phase transition", "emergent", "self-organization", "feedback loop",
                        "percolation", "contagion", "diffusion", "synchronization",
                        "power law", "small-world", "preferential attachment"],
    "engineering": ["server", "api", "monitoring", "distributed system", "microservice", "sensor", "fusion", "multi-sensor",
                    "load balanc", "cache", "latency", "throughput", "redundancy",
                    "failover", "deployment", "infrastructure", "container", "kubernetes",
                    "ci/cd", "pipeline", "scaling", "auto-scal"],
    # Cross-domain scientific threads (added June 7 2026)
    # These catch [TRANSFER] items that would otherwise land in "other"
    "biology": ["biology", "biological", "protein", "enzyme", "cell", "dna", "rna", "gene",
                "organism", "species", "ecology", "ecosystem", "evolution", "phylogen",
                "tardigrade", "neural", "synapse", "receptor", "metabol", "physiology",
                "anatomy", "morpholog", "phenotyp", "genotyp", "clade", "taxon"],
    "materials": ["material", "alloy", "polymer", "ceramic", "composite", "crystal",
                  "lattice", "grain", "fracture", "fatigue", "corrosion", "wear",
                  "hardness", "tensile", "elastic", "visco", "rheolog", "thermal",
                  "conductiv", "diffusiv", "semiconductor", "superconductor"],
    "medicine": ["medical", "clinical", "patient", "disease", "treatment", "therapy",
                 "diagnosis", "prognosis", "biomarker", "patholog", "oncolog",
                 "cardio", "neurolog", "immun", "pharmac", "drug", "dosage",
                 "surgery", "vaccine", "epidemiolog", "public health"],
    "ecology": ["ecology", "ecosystem", "biodiversity", "population", "habitat", "carbon", "agriculture", "soil",
                "conservation", "climate", "weather", "ocean", "marine", "forest",
                "soil", "plant", "animal", "pollinat", "symbio", "parasit",
                "predator", "prey", "food web", "trophic"],
    "physics": ["physics", "quantum", "thermodynamic", "entropy", "energy",
                "wave", "optics", "photon", "electron", "magnetic", "gravit",
                "fluid", "turbulen", "plasma", "nuclear", "particle",
                "relativity", "cosmolog", "astrophys"],
    "chemistry": ["chemistry", "chemical", "reaction", "catalyst", "molecular",
                  "bond", "solvent", "pH", "acid", "base", "oxid", "reduc",
                  "polymer", "monomer", "compound", "element", "isotope",
                  "spectrosc", "chromatog", "titration"],
    "economics": ["economic", "market", "price", "trading", "financial", "fuel", "cost model", "transportation cost",
                  "inflation", "interest rate", "GDP", "supply", "demand",
                  "auction", "game theory", "behavioral econ", "utility",
                  "risk", "portfolio", "insurance"],
    "linguistics": ["linguistic", "syntax", "semantics", "phonolog", "morpholog",
                    "grammar", "language model", "token", "vocabulary",
                    "corpus", "discourse", "pragmatics", "sociolinguistic",
                    "speech", "formant", "intelligibility", "kinship",
                    "language famil", "wikipedia", "edit campaign", "acoustic"],
}

# Underrepresented threads that deserve more exploration
ORTHOGONAL_THREADS = [
    "hardware", "domain", "cross_pollination", "consistency",
    "meta_learning", "workspace", "cross_lingual", "metacognition",
]

# Template questions for orthogonal exploration (by thread)
ORTHOGONAL_QUESTIONS = {
    "hardware": [
        "What is the minimum hardware required for real-time injection detection at edge scale?",
        "How does quantization (INT8/INT4) affect detector accuracy vs latency tradeoff?",
        "Can SIMD-optimized cosine similarity replace embedding models for production detection?",
    ],
    "domain": [
        "Does injection detection transfer across languages without retraining?",
        "What is the minimum cross-domain F1 achievable with a universal detector?",
        "How does domain shift affect calibration of injection classifiers?",
    ],
    "cross_pollination": [
        "[TRANSFER] Can spectral features from audio adversarial detection improve text injection detection?",
        "[TRANSFER] Does inverted signal detection (features that DECREASE for anomaly) work for prompt injection?",
        "[TRANSFER] Can PCA subspace detection from adversarial ML transfer to injection detection?",
    ],
    "consistency": [
        "What is the variance of TF-IDF+LR predictions across random seeds?",
        "Do injection detectors produce consistent results across different tokenizers?",
        "How stable are detector decision boundaries under data augmentation?",
    ],
    "meta_learning": [
        "Can the agent predict which experiments will be refuted before running them?",
        "What is the optimal batch size for curiosity queue scoring?",
        "How does experiment frequency affect synthesis quality?",
    ],
    "workspace": [
        "What is the optimal TTL for experiment workspace files?",
        "How many concurrent workspaces can the system sustain before disk pressure?",
        "What is the cost of workspace archival vs deletion?",
    ],
    "cross_lingual": [
        "Can a TF-IDF+LR classifier trained on English injection data detect injections in French/Spanish/Chinese?",
        "What is the minimum training data per language to achieve F1>0.8 cross-lingually?",
        "Does character n-gram overlap between languages predict cross-lingual transfer performance?",
        "Can code-mixed (English+Language) inputs bypass injection detectors trained on monolingual data?",
        "What is the information density of cross-lingual injection detection vs monolingual?",
    ],
    "metacognition": [
        "Can the agent predict which of its own experiments will be SUPPORTED vs REFUTED before running them?",
        "What is the optimal number of concurrent workers to maximize research throughput per dollar?",
        "Does the agent exhibit confirmation bias in which hypotheses it chooses to test?",
        "Can we build a meta-predictor that estimates experiment quality BEFORE running it?",
    ],
}


# Diminishing returns indicators in experiment results
DIMINISHING_SIGNALS = [
    "refuted", "no improvement", "no gain", "already optimal", "ceiling",
    "no change", "plateau", "marginal", "never worth", "not worth",
    "no benefit", "h1 rejected", "already saturated", "diminishing",
    "routing never", "inherently robust", "surprisingly robust",
]

# Positive signals in experiment results
POSITIVE_SIGNALS = [
    "breakthrough", "novel", "first time", "new finding", "surprising",
    "contrary to expectation", "paradigm", "fundamental", "universal",
    "generalizes", "confirmed", "supported", "validated",
]


def classify_thread(text: str, source_domain: str = None) -> str:
    """Classify text into a research thread.
    
    source_domain: if provided and text is a [TRANSFER] item with no real
    keyword match (only the [transfer] tag itself), use this as fallback.
    Routes transfers back to source domain, creating bilateral edges.
    """
    text_lower = text.lower()
    # Strip [transfer] tag for keyword matching — it's a tag, not a topic
    text_content = re.sub(r'\[transfer(?:\s+from\s+\w+)?\]\s*', '', text_lower).strip()
    for thread, keywords in THREAD_KEYWORDS.items():
        if thread == "cross_pollination":
            continue  # Skip the [transfer] keyword itself — it's a tag
        if any(kw in text_content for kw in keywords):
            return thread
    # Fallback for [TRANSFER] items: route back to source domain if available
    # This creates bilateral edges instead of dumping everything into the hub
    if "[transfer]" in text_lower and source_domain:
        return source_domain
    # Final fallback
    if "[transfer]" in text_lower:
        return "cross_pollination"
    return "other"


def load_state():
    """Load self_state.json."""
    with open(SELF_STATE_PATH) as f:
        return json.load(f)


def build_experiment_index(state):
    """Build lookup from experiment ID to result text.

    Merges both self_state.json AND prometheus.db to catch all completed
    experiments (self_state often lags behind the DB by hundreds of experiments).

    Uses a 5-minute TTL cache to avoid rebuilding the 14K+ experiment index
    on every call. Cache is invalidated when stale.
    """
    import time
    now = time.time()

    # Return cached index if fresh
    if (_EXP_INDEX_CACHE["index"] is not None and
            now - _EXP_INDEX_CACHE["timestamp"] < _EXP_INDEX_TTL):
        return _EXP_INDEX_CACHE["index"]

    exp_index = {}

    # Source 1: self_state.json
    experiments = state.get("experiments", {})
    if isinstance(experiments, dict):
        completed = experiments.get("completed", [])
    else:
        completed = state.get("metrics", {}).get("experiments_completed_list", [])
    for exp in completed:
        if isinstance(exp, dict):
            eid = exp.get("id", exp.get("name", ""))
            result = exp.get("result", "")
            hypothesis = exp.get("hypothesis", "")
        elif isinstance(exp, str):
            eid = exp
            result = ""
            hypothesis = ""
        else:
            continue
        if eid:
            exp_index[eid] = {
                "result": str(result),
                "hypothesis": str(hypothesis),
                "thread": classify_thread(eid + " " + str(hypothesis) + " " + str(result)),
            }

    # Source 2: prometheus.db (richer data, more complete)
    try:
        import sqlite3
        db_path = os.path.expanduser("~/.hermes/prometheus.db")
        db = sqlite3.connect(db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=3000")
        rows = db.execute(
            "SELECT id, hypothesis, result FROM experiments WHERE status='completed'"
        ).fetchall()
        db.close()
        for eid, hyp, res in rows:
            if eid and eid not in exp_index:
                exp_index[eid] = {
                    "result": str(res or ""),
                    "hypothesis": str(hyp or ""),
                    "thread": classify_thread(eid + " " + str(hyp or "") + " " + str(res or "")),
                }
            elif eid and eid in exp_index:
                # Prefer DB data (richer)
                if not exp_index[eid].get("hypothesis") and hyp:
                    exp_index[eid]["hypothesis"] = str(hyp)
                if not exp_index[eid].get("result") and res:
                    exp_index[eid]["result"] = str(res)
    except Exception:
        pass  # DB unavailable — fall back to self_state only

    # Update cache
    _EXP_INDEX_CACHE["index"] = exp_index
    _EXP_INDEX_CACHE["timestamp"] = now

    # Pre-compute word sets for fast dedup (avoids regex on every hypothesis for every queue item)
    _EXP_INDEX_CACHE["word_sets"] = {
        eid: set(re.findall(r'\\w{4,}', (data.get('hypothesis') or '').lower()))
        for eid, data in exp_index.items()
        if data.get('hypothesis')
    }

    return exp_index


def get_recent_threads(exp_index, n=RECENT_N):
    """Get threads of the N most recent experiments WITH HYPOTHESES (by ID number).

    Skips experiments with no hypothesis/result — they can't be classified
    and would all fall into 'other', distorting novelty calculations.
    """
    # Sort by experiment number, filter for ones with content
    def exp_num(eid):
        m = re.search(r"exp_(\d+)", eid)
        return int(m.group(1)) if m else 0

    # Only consider experiments that have a hypothesis or result
    classified = {
        eid: data for eid, data in exp_index.items()
        if data.get("hypothesis") or data.get("result")
    }
    sorted_exps = sorted(classified.keys(), key=exp_num, reverse=True)[:n]
    return [classified[eid]["thread"] for eid in sorted_exps]


def thread_experiment_count(exp_index, thread):
    """Count how many experiments exist in a thread."""
    return sum(1 for v in exp_index.values() if v["thread"] == thread)


def compute_novelty_score(item_thread, recent_threads):
    """
    Score 0-10. Rewards NOVELTY (under-represented threads get higher scores).

    exp_72526584 found recency bias: injection_detection 2.66x self-reinforcing.
    The previous "reward familiarity" approach amplified this loop.
    Now: items in threads that appear LESS in recent experiments score HIGHER.

    10 = completely novel thread (not in recent experiments)
    0 = heavily represented thread (all recent experiments in same thread)

    FIX (June 6 2026): "other" is a catch-all bucket for items that don't
    match any keyword thread. It naturally dominates recent experiments,
    making all "other" items score novelty=0. But "other" items are actually
    diverse (different topics that just lack keyword matches). Give "other"
    a neutral baseline of 5 instead of penalizing it for being the default.
    """
    if not recent_threads:
        return 5  # neutral if no history

    # "other" is a catch-all, not a real research thread.
    # Don't penalize it for appearing in all recent experiments.
    if item_thread == "other":
        return 5  # Neutral — "other" is inherently diverse

    # How many of the last N experiments are in the same thread?
    same_count = sum(1 for t in recent_threads if t == item_thread)
    ratio = same_count / len(recent_threads)

    # Novelty: all same = 0 (over-represented), none same = 10 (novel)
    return int(10 * (1 - ratio))


def compute_diminishing_returns_score(item_text, source_exp_id, exp_index, thread_count, thread=""):
    """
    Score 0-25. Lower = more diminishing returns.

    PERCENTILE-BASED (exp_72526550): Fixed thresholds broke at 9000+ scale
    where every thread has 26-7493 experiments. Now uses percentile ranking
    within the thread count distribution.

    Current distribution (14 threads, 9072 experiments):
      p25=26, p50=78, p75=279, p90=522, max=7493
    """
    score = 25  # Start at max

    # Source experiment quality
    if source_exp_id and source_exp_id in exp_index:
        result = exp_index[source_exp_id]["result"].lower()
        for signal in DIMINISHING_SIGNALS:
            if signal in result:
                score -= 12
                break

    # Thread saturation: percentile-based penalty
    # Use the pre-computed thread_count from the caller instead of
    # recalculating from exp_index (which is expensive and could give
    # inconsistent results across calls).
    # Compute percentile from all thread counts in exp_index
    from collections import Counter as _C
    all_thread_counts = list(_C(v["thread"] for v in exp_index.values()).values())
    if all_thread_counts:
        below = sum(1 for c in all_thread_counts if c < thread_count)
        percentile = below / len(all_thread_counts)
        # Top 20% = max penalty (-18), bottom 20% = no penalty
        if percentile > 0.8:
            score -= 18  # Top 20% most saturated
        elif percentile > 0.6:
            score -= 14
        elif percentile > 0.4:
            score -= 10
        elif percentile > 0.2:
            score -= 6
        # Bottom 20% = no penalty (0)

    # Domain saturation penalty: threads with many experiments are over-invested
    # (exp_72526528: injection detection confirmed shallow enough for 1B models)
    # exp_72526583: security_injection has NEGATIVE MI despite being largest domain)
    # Thread counts: injection=489, tfidf=279, other=7493, cross_lingual=57
    #
    # FIX (June 6 2026): "other" is a catch-all bucket, not a real research
    # thread. Its high count reflects classification noise, not over-investment.
    # Don't penalize "other" for thread saturation.
    if thread != "other":
        if thread_count > 500:
            score -= 10  # Over-invested — redirect capacity elsewhere
        elif thread_count > 200:
            score -= 5   # Heavily invested

    # Check if this is re-asking the same question
    if source_exp_id and source_exp_id in exp_index:
        source_hyp = exp_index[source_exp_id]["hypothesis"].lower()
        item_lower = item_text.lower()
        source_words = set(re.findall(r"\w+", source_hyp))
        item_words = set(re.findall(r"\w+", item_lower))
        if source_words and item_words:
            overlap = len(source_words & item_words) / max(len(source_words | item_words), 1)
            if overlap > 0.7:
                score -= 12  # High overlap = re-asking same question

    return max(0, min(25, score))


def compute_source_quality_score(source_exp_id, exp_index):
    """
    Score 0-50. DOMINANT signal — only component that predicts success (r=+0.18).

    This is the core of the rescoring. Previous version capped at 40 and
    most items got 10 (unknown source) or 30 (normal results). Now:
    - 50 = strong positive results (breakthrough, novel, confirmed)
    - 35 = normal results with findings
    - 25 = unknown source (neutral baseline, not penalized)
    - 15 = partial/interrupted results
    - 10 = minimal results
    """
    if not source_exp_id or source_exp_id not in exp_index:
        return 25  # Unknown source — neutral baseline

    result = exp_index[source_exp_id]["result"].lower()

    # Partial/interrupted results
    if any(w in result for w in ["partial", "interrupted", "incomplete", "truncated"]):
        return 15

    # Strong positive results
    if any(w in result for w in POSITIVE_SIGNALS):
        return 50

    # Normal results (has findings)
    if len(result) > 50:
        return 35

    return 10  # Minimal results


def compute_diversity_bonus(thread, thread_count, all_thread_counts=None):
    """
    Score 0-10. Rewards UNDER-REPRESENTED threads (rare threads get higher scores).

    Inverted from previous version (June 7 2026). The old version rewarded
    popular threads, creating a self-reinforcing loop where popular topics
    dominated the queue. Now: items in threads with FEW queue entries score
    HIGHER, encouraging exploration of underserved domains.

    10 = thread not in queue at all (completely novel)
    5 = thread with few entries (under-represented)
    0 = most popular thread in queue (over-represented)

    The "other" catch-all gets a neutral baseline of 5.
    """
    if thread_count == 0:
        # Thread not in queue — maximum novelty bonus
        if all_thread_counts and len(all_thread_counts) > 5:
            return 10  # Queue has many explored threads, this one is novel
        return 5  # Neutral — unexplored

    if all_thread_counts is None:
        # Fallback: inverse log-scale
        import math
        log_count = math.log2(max(thread_count, 1))
        # Invert: low count = high score
        return max(0, min(10, int(10 * (1 - log_count / 7))))

    # Percentile-based INVERTED: where does this thread fall in the distribution?
    below = sum(1 for c in all_thread_counts if c < thread_count)
    percentile = below / max(len(all_thread_counts), 1)

    # INVERTED: 0th percentile (least popular) = 10, 100th (most popular) = 0
    return max(0, min(10, int(10 * (1 - percentile))))


def is_resolved(item_text):
    """Check if queue item is already resolved."""
    return "[RESOLVED" in item_text or "[ANSWERED" in item_text


def is_already_in_queue(item_text, existing_queue):
    """Check if this item is a near-duplicate of something already in the queue.

    Uses word overlap against existing queue items to prevent duplicates.
    Returns True if an existing item has >0.55 overlap (lowered from 0.7
    to catch more near-duplicates like "defense-depth" 49x repeats).
    """
    item_words = set(re.findall(r'\w{4,}', item_text.lower()))
    if len(item_words) < 3:
        return False

    for existing in existing_queue:
        existing_text = str(existing).lower()
        existing_words = set(re.findall(r'\w{4,}', existing_text))
        if not existing_words:
            continue
        overlap = len(item_words & existing_words) / max(len(item_words | existing_words), 1)
        if overlap > 0.55:
            return True
    return False


def is_already_answered(item_text, exp_index, item_dict=None):
    """Check if this question was already answered by a completed experiment.

    Compares the queue item's text against completed experiment hypotheses.
    If significant word overlap exists, the question has already been investigated.
    Lowered threshold from 0.6 to 0.5 to catch more near-duplicates.

    NEW: Skip check for ANALOGICAL items — they are explicitly new cross-domain
    questions derived from completed experiments, so word overlap is expected.

    Uses pre-computed word sets from cache (avoids O(n×m) regex comparisons).
    """
    # ANALOGICAL items are new questions by definition — don't filter them
    if item_dict and item_dict.get("experiment_type") == "ANALOGICAL":
        return False

    item_words = set(re.findall(r'\w{4,}', item_text.lower()))
    if len(item_words) < 3:
        return False

    # Use pre-computed word sets if available (fast path)
    cached_word_sets = _EXP_INDEX_CACHE.get("word_sets")
    if cached_word_sets:
        for eid, hyp_words in cached_word_sets.items():
            if not hyp_words:
                continue
            overlap = len(item_words & hyp_words) / max(len(item_words | hyp_words), 1)
            if overlap > 0.5:
                return True
        return False

    # Fallback: compute on the fly (for backward compatibility)
    for exp_id, exp_data in exp_index.items():
        hypothesis = (exp_data.get('hypothesis') or '').lower()
        if not hypothesis:
            continue
        hyp_words = set(re.findall(r'\w{4,}', hypothesis))
        if not hyp_words:
            continue
        overlap = len(item_words & hyp_words) / max(len(item_words | hyp_words), 1)
        if overlap > 0.5:
            return True
    return False


def score_item(item_text, exp_index, recent_threads, source_domain=None, item_dict=None, queue_thread_freq=None, source_fanout_counts=None):
    """Compute total priority score for a queue item.

    RESCORED (exp_72526550): source quality dominates, novelty/diversity
    inverted to reward familiarity, no thread-based bonuses.
    OUTCOME-AWARE (June 9 2026): added outcome_bonus from edge success rates.

    Formula:
      source_quality:      0-50 (dominant, only positive predictor)
      diminishing:         0-25 (inverted — LOW diminishing returns = HIGH score)
      outcome_bonus:       0-25 (NEW: from edge success rates via outcome_routing.py)
      novelty:             0-10 (rewards novel/under-represented threads)
      diversity:           0-10 (rewards under-represented threads in queue)
      transfer_bonus:      +3 (cross-domain [TRANSFER] items)
      analogical_bonus:    0-20 (ANALOGICAL experiment type bonus)
      cross_calibration:   +8 (calibration-adjacent from non-calibration domains)
      export_bonus:        +10 (high-quality stranded findings)
      total cap:           100

    source_domain: domain that generated this item. Used to route [TRANSFER]
    items back to their source, creating bilateral edges.
    item_dict: the original queue item dict, used to check experiment_type.
    queue_thread_freq: pre-computed Counter of thread frequencies (avoids O(n²) recomputation).
    """
    # Skip resolved items
    if is_resolved(item_text):
        return {
            "text": item_text[:120],
            "thread": "resolved",
            "total": 0,
            "novelty": 0,
            "diminishing": 0,
            "source_quality": 0,
            "diversity": 0,
            "resolved": True,
        }

    # Skip items already answered by completed experiments
    if is_already_answered(item_text, exp_index, item_dict=item_dict):
        return {
            "text": item_text[:120],
            "thread": "already_answered",
            "total": 0,
            "novelty": 0,
            "diminishing": 0,
            "source_quality": 0,
            "diversity": 0,
            "resolved": True,
        }

    # Source weight: synthesis-generated items scored at 0.6x
    is_synthesis = "[new from synthesis" in item_text.lower()
    source_weight = 0.6 if is_synthesis else 1.0

    # Classify thread — pass source_domain so [TRANSFER] items route back
    thread = classify_thread(item_text, source_domain=source_domain)
    thread_count = thread_experiment_count(exp_index, thread)

    # Extract source experiment ID
    source_match = re.search(r"exp_(\d+\w*)", item_text)
    source_exp_id = f"exp_{source_match.group(1)}" if source_match else None

    # Source-experiment fan-out cooldown (June 2026):
    # When one experiment generates many curiosities, they all score highly
    # and crowd out other topics — a self-reinforcing feedback loop.
    # Penalize items whose source experiment has too many active queue items.
    COOLDOWN_THRESHOLD = 2  # max active items per source before penalty kicks in
    source_fanout_penalty = 0
    if source_exp_id and source_fanout_counts is not None:
        fanout_count = source_fanout_counts.get(source_exp_id, 0)
        if fanout_count > COOLDOWN_THRESHOLD:
            # Penalty scales linearly: -5 per item above threshold
            source_fanout_penalty = -5 * (fanout_count - COOLDOWN_THRESHOLD)

    # Compute components
    novelty = compute_novelty_score(thread, recent_threads)

    # Claim registry integration: zero out downgraded components
    _statuses = load_claim_statuses()
    _novelty_claim = _statuses.get("novelty", {})
    if _novelty_claim.get("status") == "downgraded":
        novelty = 0  # novelty scoring not predictive — audit June 12 2026

    diminishing = compute_diminishing_returns_score(
        item_text, source_exp_id, exp_index, thread_count, thread=thread
    )
    source_quality = compute_source_quality_score(source_exp_id, exp_index)
    # Claim registry: zero out source_quality if source quality claim is downgraded
    _sq_claim = _statuses.get("source_quality", {})
    if _sq_claim.get("status") == "downgraded":
        source_quality = 0  # source quality not predictive — audit June 12 2026

    # Compute thread distribution for percentile-based diversity
    # Use pre-computed queue_thread_freq if provided (O(n)), else fallback to O(n²) recomputation
    if queue_thread_freq is not None:
        queue_count_for_thread = queue_thread_freq.get(thread, 0)
        diversity = compute_diversity_bonus(thread, queue_count_for_thread, list(queue_thread_freq.values()))

    # Claim registry: zero out diversity if downgraded
    _diversity_claim = _statuses.get("diversity", {})
    if _diversity_claim.get("status") == "downgraded":
        diversity = 0  # diversity scoring not validated — audit June 12 2026

    else:
        # Fallback: recompute (for backward compatibility with direct callers)
        try:
            _state = load_state()
            queue_items = _state.get('curiosity_queue', [])
        except Exception:
            queue_items = []
        queue_threads = [classify_thread(
                            str(item) if not isinstance(item, dict) else item.get('text', item.get('question', str(item))),
                            source_domain=item.get('source_domain') if isinstance(item, dict) else None
                        ) for item in queue_items]
        from collections import Counter as C
        queue_thread_freq = C(queue_threads)
        queue_count_for_thread = queue_thread_freq.get(thread, 0)
        diversity = compute_diversity_bonus(thread, queue_count_for_thread, list(queue_thread_freq.values()))

    # Cross-pollination bonus: [TRANSFER] items get a small boost
    # (reduced from +15 to +3 — transfers shouldn't dominate scoring)
    transfer_bonus = 3 if "[transfer]" in item_text.lower() else 0

    # FIX (2026-06-18): transfer_tracking pipeline fixed — transfer_bonus stays active.
    # The governance downgrade is no longer in effect.


    # Domain health bonus: adjust transfer bonus based on target domain's role.
    # This closes the loop from governance metrics → scoring → behavior.
    # Producer/hub domains get boosted (more likely to produce follow-on work).
    # Transient domains get penalized (wasting transfer cycles).
    #
    # SAFETY: bonus is capped to prevent runaway exploitation.
    # An exploration budget ensures under-represented domains still get attention.
    domain_health_bonus = 0
    # Claim registry: zero out domain_health_bonus if domain taxonomy is downgraded
    _dt_claim = _statuses.get("domain_taxonomy", {})
    if _dt_claim.get("status") == "downgraded":
        domain_health_bonus = 0  # domain taxonomy dead — audit June 12 2026
    if "[transfer]" in item_text.lower() and source_domain:
        try:
            _dhc_path = os.path.join(os.path.expanduser("~"),
                                     ".hermes", "domain_health_cache.json")
            if os.path.exists(_dhc_path):
                with open(_dhc_path) as _f:
                    _dhc = json.load(_f)
                _target_info = _dhc.get("domains", {}).get(source_domain, {})
                _role = _target_info.get("role", "")
                if _role in ("producer", "hub"):
                    domain_health_bonus = 5  # boost transfers to productive domains
                elif _role == "reservoir":
                    domain_health_bonus = 3  # reservoirs export well, worth feeding
                elif _role == "transient":
                    domain_health_bonus = -2  # penalize transfers to dying domains
                elif _role == "consumer":
                    domain_health_bonus = -1  # slight penalty — consumes but doesn't produce

                # EXPLORATION BUDGET: if target domain is in the bottom 30% by
                # experiment count, override the penalty. This prevents the system
                # from starving small but potentially valuable domains.
                _total = _target_info.get("total", 0)
                if _total < 10 and domain_health_bonus < 0:
                    domain_health_bonus = 0  # don't penalize small domains — they need exploration
        except Exception:
            pass  # cache read failures don't affect scoring

    # Cross-domain calibration bonus: encourage calibration-adjacent research
    # from non-calibration domains. Reduces dependency on calibration as sole
    # source for confidence/uncertainty research.
    calibration_keywords = ["confidence", "uncertainty", "reliability", "calibration",
                            "probability", "prediction interval", "conformal", "trust"]
    is_calibration_domain = source_domain == "calibration"
    touches_calibration = any(kw in item_text.lower() for kw in calibration_keywords)
    cross_calibration_bonus = 8 if (touches_calibration and not is_calibration_domain) else 0

    # ANALOGICAL novelty bonus: embedding-based cross-domain distance
    # Items with experiment_type=ANALOGICAL get a 0-20 bonus based on novelty_score
    # UNIVERSAL_LAW sources get 1.3x multiplier (higher expected transfer success)
    analogical_bonus = 0
    if item_dict and item_dict.get("experiment_type") == "ANALOGICAL":
        novelty_score = item_dict.get("novelty_score", 0.5)
        mechanism_type = item_dict.get("mechanism_type", "EMPIRICAL_CORRELATION")
        mechanism_multiplier = 1.3 if mechanism_type == "UNIVERSAL_LAW" else 1.0
        # Scale 0-1 novelty to 0-20 bonus, then apply mechanism multiplier
        analogical_bonus = int(novelty_score * 20 * mechanism_multiplier)
        # Claim registry: zero out analogical_bonus if novelty injection is downgraded
        _ni_claim = _statuses.get("novelty", {})
        if _ni_claim.get("status") == "downgraded":
            analogical_bonus = 0  # novelty injection not validated — audit June 12 2026

    # Export bonus: reward high-novelty zero-transfer findings
    # Stranded findings (high quality, no outbound transfers) get boosted
    # to pressure the system toward abstraction extraction
    export_bonus = 0

    # Outcome bonus: reward edges with high success rates
    # Loaded from outcome_routing.py cache (flow + outcome + novelty scores)
    outcome_bonus = 0
    try:
        _outcome_cache_path = os.path.join(os.path.expanduser("~"), ".hermes", "routing_outcome_cache.json")
        if os.path.exists(_outcome_cache_path):
            with open(_outcome_cache_path) as _f:
                _outcome_cache = json.load(_f)
            # Try to match item text to a source->target edge
            # Look for domain names in the item text
            _best_score = 0
            for _key, _data in _outcome_cache.items():
                _parts = _key.split("->")
                if len(_parts) == 2:
                    _src, _tgt = _parts
                    # Check if both domains appear in the item text (fuzzy)
                    _src_words = _src.replace("_", " ").split()
                    _tgt_words = _tgt.replace("_", " ").split()
                    _text_lower = item_text.lower()
                    _src_match = sum(1 for w in _src_words if w in _text_lower) >= len(_src_words) * 0.5
                    _tgt_match = sum(1 for w in _tgt_words if w in _text_lower) >= len(_tgt_words) * 0.5
                    if _src_match and _tgt_match:
                        _score = _data.get("routing_score", 0)
                        if _score > _best_score:
                            _best_score = _score
            # Scale 0-1 routing_score to 0-40 outcome_bonus (strategy reversal — higher pushes hidden bridges above established routes)
            outcome_bonus = int(_best_score * 46)
    except Exception:
        pass
    # Boost [STRANDED] items directly — these are manually injected high-priority findings
    if "[stranded" in item_text.lower():
        export_bonus = 10
    try:
        import sqlite3 as _sql
        _db_path = os.path.join(os.path.expanduser("~"), ".hermes", "prometheus.db")
        _conn = _sql.connect(_db_path)
        _export_found = False
        # Check source_exp_id directly
        if source_exp_id and not _export_found:
            _row = _conn.execute(
                "SELECT quality_score, tags FROM experiments WHERE id = ?",
                (source_exp_id,)
            ).fetchone()
            if _row and _row[0] and _row[0] >= 70:
                _tags = _row[1] or ""
                if "[transfer]" not in _tags.lower():
                    export_bonus = 10
                    _export_found = True
        # Also check if item text references an experiment ID (like "exp_091_v2")
        if not _export_found:
            _exp_match = re.search(r'exp_(\d+)', item_text.lower())
            if _exp_match:
                _exp_id = f"exp_{_exp_match.group(1)}"
                _row = _conn.execute(
                    "SELECT quality_score, tags FROM experiments WHERE id LIKE ?",
                    (f"%{_exp_id}%",)
                ).fetchone()
                if _row and _row[0] and _row[0] >= 70:
                    _tags = _row[1] or ""
                    if "[transfer]" not in _tags.lower():
                        export_bonus = 10
                        _export_found = True
        _conn.close()
    except Exception:
        pass

    # Replication penalty: reduce score for items from domains with high disagreement rates
    # Reads from replication_state.json (computed by replication_tracker.py), with a
    # freshness guard: if that snapshot is stale/missing the scorer falls back to the
    # live DB column instead of silently trusting old domain scores.
    replication_penalty = 0
    try:
        _repl, _repl_stale = load_replication_state()
        _rates = _repl.get("domain_disagreement_rates", {})
        _disagreed_ids = _repl.get("disagreed_experiment_ids", [])
        if _repl_stale:
            # snapshot down → recompute this domain's rate + disagreed ids live
            _live = live_domain_disagreement(source_domain)
            if _live is not None:
                _rates = {source_domain: _live}
            _disagreed_ids = live_disagreed_experiment_ids()
        # Check if source_domain has high disagreement rate
        if source_domain and source_domain in _rates:
            _rate = _rates[source_domain].get("disagreement_rate", 0)
            _total = _rates[source_domain].get("total", 0)
            if _total >= 2 and _rate >= 0.5:
                replication_penalty = -15  # heavy penalty for >50% disagreement
            elif _total >= 2 and _rate >= 0.33:
                replication_penalty = -8   # moderate penalty for >33% disagreement
        # Also check if item references a specific disagreed experiment
        if source_exp_id and source_exp_id in _disagreed_ids:
            replication_penalty = -20  # maximum penalty for specifically disagreed experiment
    except Exception:
        pass

    # ── Break informativeness bonus ──
    # Rewards domains where replication disagreements revealed STRUCTURAL breaks.
    # High break_informativeness means the domain produces boundary-signal, not just
    # match-counting. This corrects the scorer's historical bias toward rewarding
    # successful transfers over informative failures.
    break_bonus = 0
    try:
        _repl, _repl_stale = load_replication_state()
        _break_scores = _repl.get("domain_break_informativeness", {})
        # Resolve this domain's break score: prefer the snapshot; on staleness,
        # fall back to the live replication_results column so a dead refresher
        # doesn't silently freeze break scoring.
        _dom_break = _break_scores.get(source_domain) if source_domain else None
        if _repl_stale and source_domain:
            _live = live_domain_break_score(source_domain)
            if _live is not None:
                _dom_break = _live
        if _dom_break:
            _avg = _dom_break.get("avg_informativeness", 0)
            _count = _dom_break.get("break_count", 0)
            if _avg >= 15 and _count >= 2:
                break_bonus = 12  # strong boundary-signal domain
            elif _avg >= 10 and _count >= 1:
                break_bonus = 6   # moderate boundary-signal
            elif _avg >= 5:
                break_bonus = 3   # emerging boundary-signal
        # Penalize domains where all breaks are low-informativeness
        # (magnitude_only disagreements — suggests umbrella matching, not
        #  genuine boundary finding)
        _all_breaks = _dom_break or {}
        _all_avg = _all_breaks.get("avg_informativeness", 0)
        _all_count = _all_breaks.get("break_count", 0)
        if _all_count >= 3 and _all_avg < 8:
            break_bonus = -5  # break-poor domain — deprioritize
        # Also penalize domains with conjectured (unverified limit) claims.
        # (conjectured counts come from the snapshot only; when stale this just
        #  no-ops rather than fabricating a penalty.)
        _conj = _repl.get("conjectured_claims", 0)
        if _conj > 0 and source_domain:
            _conj_domains = _repl.get("conjectured_domains", {})
            if source_domain in _conj_domains:
                break_bonus -= 3  # extra penalty for unverified limit claims
    except Exception:
        pass

    # Knowledge claim penalty: penalize items whose underlying claim is CONTESTED or RETIRED
    # This is the missing link between evidence accumulation and belief revision.
    # If a hypothesis has been tested many times with mixed results, new experiments
    # testing the same claim should be deprioritized.
    claim_penalty = 0
    claim_status_info = None
    try:
        import sqlite3 as _sql
        _db_path = os.path.join(os.path.expanduser("~"),
                                 ".hermes", "prometheus.db")
        _conn = _sql.connect(_db_path, timeout=3)
        _conn.execute("PRAGMA busy_timeout=2000")
        from claim_lifecycle import hypothesis_hash
        
        # Strategy 1: direct text lookup (item text IS the hypothesis or close to it)
        # Priority: direct text is more likely to match the actual claim
        _claim = None
        _h = hypothesis_hash(item_text)
        _claim = _conn.execute(
            "SELECT id, status, posterior, support_count, refute_count, total_evidence "
            "FROM knowledge_claims WHERE claim_hash = ?",
            (_h,)
        ).fetchone()
        
        # Strategy 2: look up via source experiment's hypothesis (fallback)
        if not _claim and source_exp_id:
            _exp = _conn.execute(
                "SELECT hypothesis FROM experiments WHERE id = ?",
                (source_exp_id,)
            ).fetchone()
            if _exp and _exp[0]:
                _h = hypothesis_hash(_exp[0])
                _claim = _conn.execute(
                    "SELECT id, status, posterior, support_count, refute_count, total_evidence "
                    "FROM knowledge_claims WHERE claim_hash = ?",
                    (_h,)
                ).fetchone()
        
        if _claim:
            claim_status_info = {
                "claim_id": _claim[0],
                "status": _claim[1],
                "posterior": _claim[2],
                "support": _claim[3],
                "refute": _claim[4],
                "total": _claim[5],
            }
            # Check both status systems: old RETIRED/CONTESTED + new DISPUTED
            # Also query claim_status column to catch new-system disputes
            _new_status = _conn.execute(
                "SELECT claim_status FROM knowledge_claims WHERE id = ?",
                (_claim[0],)
            ).fetchone()
            _is_disputed = _new_status and _new_status[0] == 'DISPUTED'
            
            if _claim[1] == "RETIRED" or _is_disputed:
                claim_penalty = -25  # strongly refuted/disputed — don't revisit
            elif _claim[1] == "CONTESTED":
                claim_penalty = -10  # mixed evidence — reduce priority
            elif _claim[1] == "ACTIVE" and _claim[5] >= 5:
                claim_penalty = 5    # well-supported — worth building on
        _conn.close()
    except Exception:
        pass

    # Evidence depth bonus (Hypothesis P-001): questions with more experimental
    # ancestors are more fertile. Boost questions grounded in experimental evidence.
    # Depth 0 = +0, Depth 1 = +3, Depth 2 = +6, Depth 3 = +9, capped at +15.
    evidence_depth_bonus = 0
    try:
        _ed = item.get("evidence_depth", 0) if isinstance(item, dict) else 0
        evidence_depth_bonus = min(_ed * 3, 15)
    except Exception:
        pass

    total = novelty + diminishing + source_quality + outcome_bonus + diversity + transfer_bonus + analogical_bonus + cross_calibration_bonus + export_bonus + domain_health_bonus + break_bonus + replication_penalty + claim_penalty + evidence_depth_bonus + source_fanout_penalty

    # Self-referentiality penalty (June 17 2026): WR #81130 found that
    # self-referential source domains reduce transfer success by 5.1pp
    # (63.1% vs 68.2%, p<0.0001). Apply a 0.85x multiplier to items from
    # domains that study the system's own mechanisms, breaking the circular
    # curiosity loop that amplifies miscalibration.
    SELF_REFERENTIAL_DOMAINS = {
        'meta_analysis', 'calibration', 'cross_domain_transfer',
        'cross_domain_prediction', 'bias', 'cognitive_science',
        'cognitive_bias', 'meta_learning', 'ai_alignment', 'safety',
        'system_health', 'metrics_benchmarking', 'epistemic_architecture',
        'adversarial_ml'  # system studying its own attack surfaces
    }
    selfref_penalty = 0
    if source_domain and source_domain in SELF_REFERENTIAL_DOMAINS:
        selfref_penalty = int(total * -0.15)  # 15% score reduction
    total += selfref_penalty

    # Apply source weight — synthesis items penalized
    total = int(total * source_weight)

    # Cap at 100
    total = min(100, max(0, total))

    return {
        "text": item_text[:120],
        "thread": thread,
        "total": total,
        "novelty": novelty,
        "diminishing": diminishing,
        "source_quality": source_quality,
        "outcome_bonus": outcome_bonus,
        "diversity": diversity,
        "transfer_bonus": transfer_bonus,
        "analogical_bonus": analogical_bonus,
        "cross_calibration_bonus": cross_calibration_bonus,
        "export_bonus": export_bonus,
        "domain_health_bonus": domain_health_bonus,
        "break_bonus": break_bonus,
        "replication_penalty": replication_penalty,
        "claim_penalty": claim_penalty,
        "selfref_penalty": selfref_penalty,
        "evidence_depth_bonus": evidence_depth_bonus,
        "claim_status": claim_status_info.get("status") if claim_status_info else None,
        "claim_posterior": claim_status_info.get("posterior") if claim_status_info else None,
        "thread_count": thread_count,
        "source_exp": source_exp_id,
        "db_id": item_dict.get("db_id") if item_dict else None,
        "experiment_type": item_dict.get("experiment_type") if item_dict else None,
        "mechanism_type": item_dict.get("mechanism_type") if item_dict else None,
        "source_domain": item_dict.get("source_domain") if item_dict else None,
        "source_result_id": item_dict.get("source_result_id") if item_dict else None,
        "novelty_score": item_dict.get("novelty_score") if item_dict else None,
        "resolved": False,
    }


def score_all(state=None):
    """Score all active queue items.

    Reads from self_state.json AND prometheus.db curiosities table.
    The DB is the canonical store; self_state.json is a materialized view
    that sync_curiosity_views.py refreshes every 2 minutes.  If the sync
    lags or crashes, the DB fallback ensures the scorer still sees all
    active curiosities.
    """
    if state is None:
        state = load_state()

    exp_index = build_experiment_index(state)
    recent_threads = get_recent_threads(exp_index)
    queue = state.get("curiosity_queue", [])

    # --- DB fallback: merge active curiosities from prometheus.db ---
    # Follows the same pattern as build_experiment_index() which already
    # reads from both sources.  Items in self_state.json are preferred
    # (they may have been enriched by inject_opportunities or synthesis);
    # items only in the DB are converted to the dict format the scorer expects.
    try:
        import sqlite3
        db_path = os.path.expanduser("~/.hermes/prometheus.db")
        db = sqlite3.connect(db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=3000")
        db_rows = db.execute(
            "SELECT id, text, priority, source_experiment "
            "FROM curiosities WHERE status='active'"
        ).fetchall()
        db.close()

        # Index existing queue items by db_id for fast lookup
        ss_db_ids = set()
        for item in queue:
            if isinstance(item, dict) and "db_id" in item:
                ss_db_ids.add(item["db_id"])

        # Add DB items not already in self_state.json queue
        added = 0
        for cur_id, text, priority, source_exp in db_rows:
            if cur_id in ss_db_ids:
                continue
            # Convert numeric priority to label
            try:
                pri_num = int(priority) if priority is not None else 3
            except (ValueError, TypeError):
                pri_num = 3
            pri_label = "high" if pri_num <= 1 else ("medium" if pri_num <= 3 else "low")
            queue.append({
                "text": text,
                "source": source_exp or "unknown",
                "priority": pri_label,
                "db_id": cur_id,
            })
            added += 1

        if added:
            print(f"  DB fallback: merged {added} curiosities from prometheus.db")
    except Exception:
        pass  # DB unavailable — score what self_state.json has

    # Compute thread distribution for percentile-based diversity
    # NOTE: This was previously recomputed inside score_item for EVERY item (O(n²)).
    # Moved here to compute once and pass to score_item.
    queue_threads = [classify_thread(
                        str(item) if not isinstance(item, dict) else item.get('text', item.get('question', str(item))),
                        source_domain=item.get('source_domain') if isinstance(item, dict) else None
                    ) for item in queue]
    queue_thread_freq = Counter(queue_threads)

    # Source-experiment fan-out counts: how many active queue items per source experiment.
    # Used by score_item() to penalize over-represented sources (feedback loop prevention).
    source_fanout_counts = Counter()
    _source_re = re.compile(r"exp_(\d+\w*)")
    for item in queue:
        text = str(item) if not isinstance(item, dict) else item.get("text", item.get("question", str(item)))
        _m = _source_re.search(text)
        if _m:
            source_fanout_counts[f"exp_{_m.group(1)}"] += 1

    scored = []
    seen_texts = []  # Track seen items for intra-queue dedup
    for item in queue:
        text = str(item) if not isinstance(item, dict) else item.get("text", item.get("question", str(item)))
        # Extract source_domain for [TRANSFER] routing (bilateral edges)
        source_domain = item.get("source_domain") if isinstance(item, dict) else None
        # Intra-queue dedup: skip if we already scored a near-duplicate
        if is_already_in_queue(text, seen_texts):
            scored.append({
                "text": text[:120],
                "thread": "duplicate",
                "total": 0,
                "novelty": 0,
                "diminishing": 0,
                "source_quality": 0,
                "diversity": 0,
                "resolved": True,
            })
            continue
        seen_texts.append(text)
        result = score_item(text, exp_index, recent_threads, source_domain=source_domain, item_dict=item if isinstance(item, dict) else None, queue_thread_freq=queue_thread_freq, source_fanout_counts=source_fanout_counts)
        scored.append(result)

    # Sort by total score descending
    scored.sort(key=lambda x: x["total"], reverse=True)
    return scored


def print_report(scored):
    """Print full scoring report."""
    active = [s for s in scored if not s["resolved"]]
    resolved = [s for s in scored if s["resolved"]]

    print("=" * 70)
    print("CURIOSITY QUEUE PRIORITY REPORT (RESCORED)")
    print("=" * 70)
    print(f"Total items: {len(scored)} | Active: {len(active)} | Resolved: {len(resolved)}")
    print()

    # Thread distribution of active items
    thread_dist = Counter(s["thread"] for s in active)
    print("ACTIVE QUEUE THREAD DISTRIBUTION:")
    for thread, count in thread_dist.most_common():
        avg_score = sum(s["total"] for s in active if s["thread"] == thread) / count
        bar = "#" * count
        print(f"  {thread:15s}: {count:2d} items, avg score: {avg_score:.0f} {bar}")
    print()

    # Top 10 items
    print("TOP 10 PRIORITY ITEMS:")
    print("-" * 70)
    for i, s in enumerate(active[:10]):
        print(f"  #{i+1:2d} [Score: {s['total']:3d}] ({s['thread']})")
        print(f"       Source={s['source_quality']} Diminish={s['diminishing']} "
              f"Novelty={s['novelty']} Diversity={s['diversity']}"
              f"{' Transfer=+' + str(s.get('transfer_bonus',0)) if s.get('transfer_bonus') else ''}")
        print(f"       {s['text']}")
        if s.get("source_exp"):
            print(f"       Source: {s['source_exp']} (thread has {s.get('thread_count', '?')} experiments)")
        print()

    # Bottom 5 (lowest priority)
    print("BOTTOM 5 PRIORITY ITEMS (consider pruning):")
    print("-" * 70)
    for s in active[-5:]:
        print(f"  [Score: {s['total']:3d}] ({s['thread']})")
        print(f"       {s['text']}")
        print()

    # Recommendations
    print("RECOMMENDATIONS:")
    print("-" * 70)
    high_value = [s for s in active if s["total"] >= 60]
    low_value = [s for s in active if s["total"] < 30]

    if high_value:
        print(f"  HIGH PRIORITY ({len(high_value)} items, score >= 60):")
        threads = Counter(s["thread"] for s in high_value)
        for t, c in threads.most_common():
            print(f"    - {t}: {c} items")
        print("    → Director should create tasks for these first")

    if low_value:
        print(f"\n  LOW PRIORITY ({len(low_value)} items, score < 30):")
        threads = Counter(s["thread"] for s in low_value)
        for t, c in threads.most_common():
            print(f"    - {t}: {c} items")
        print("    → Consider pruning or deprioritizing these")

    # Thread imbalance warning
    max_thread = thread_dist.most_common(1)[0]
    if max_thread[1] > len(active) * 0.35:
        print(f"\n  ⚠ THREAD IMBALANCE: '{max_thread[0]}' has {max_thread[1]}/{len(active)} "
              f"items ({max_thread[1]/len(active)*100:.0f}%)")
        print(f"    → Cap at 30% and explore other threads")


def diversify_queue(state=None, dry_run=False):
    """
    Inject orthogonal questions when queue is homogeneous.
    Based on exp_72526491: freezing the queue increased diversity by 0.474 bits.
    This achieves similar results by injecting questions from underrepresented threads.

    Returns: dict with injection stats
    """
    if state is None:
        state = load_state()

    queue = state.get("curiosity_queue", [])
    if not queue:
        return {"injected": 0, "reason": "empty queue"}

    # Classify all queue items
    thread_counts = Counter()
    for item in queue:
        text = str(item) if not isinstance(item, dict) else item.get("text", item.get("question", str(item)))
        thread = classify_thread(text)
        thread_counts[thread] += 1

    total = len(queue)

    # Find dominant thread (>35%)
    dominant_thread = None
    dominant_count = 0
    for thread, count in thread_counts.most_common(1):
        if count > total * 0.35:
            dominant_thread = thread
            dominant_count = count

    if not dominant_thread:
        return {"injected": 0, "reason": "no thread dominates (>35%)"}

    # Find underrepresented threads
    underrepresented = []
    for t in ORTHOGONAL_THREADS:
        if thread_counts.get(t, 0) < 3:
            underrepresented.append(t)

    if not underrepresented:
        # All orthogonal threads have 3+ items, check for any with 0
        underrepresented = [t for t in ORTHOGONAL_THREADS if thread_counts.get(t, 0) == 0]

    if not underrepresented:
        return {"injected": 0, "reason": "all threads adequately represented"}

    # Select questions from underrepresented threads
    injected = []
    rng = random.Random(len(queue))  # Deterministic based on queue size

    # Target: inject enough to bring underrepresented threads to ~3 items each
    # But cap at 20% of queue size (the "20" in 80/20)
    max_inject = max(1, int(total * 0.20))

    for thread in underrepresented[:max_inject]:
        if thread in ORTHOGONAL_QUESTIONS:
            question = rng.choice(ORTHOGONAL_QUESTIONS[thread])
            # Mark as orthogonal injection
            tagged = f"[ORTHOGONAL from diversify] {question}"

            # Check for duplicates
            if not is_already_in_queue(tagged, queue):
                injected.append(tagged)

    if not injected:
        return {"injected": 0, "reason": "all candidates were duplicates"}

    if dry_run:
        return {
            "injected": len(injected),
            "dry_run": True,
            "questions": injected,
            "dominant_thread": dominant_thread,
            "dominant_pct": f"{dominant_count/total*100:.0f}%",
        }

    # Inject into queue
    queue.extend(injected)
    state["curiosity_queue"] = queue

    # Write back
    with open(SELF_STATE_PATH, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    return {
        "injected": len(injected),
        "dominant_thread": dominant_thread,
        "dominant_pct": f"{dominant_count/total*100:.0f}%",
        "new_total": len(queue),
    }


def apply_scores(scored):
    """Write scores from scored result list back to prometheus.db curiosities table.

    Only updates items that have a db_id field. Updates score, score_updated_at,
    and combined_score columns. Returns count of updated rows.
    """
    import time
    import sqlite3

    db_path = os.path.expanduser("~/.hermes/prometheus.db")
    now = time.time()
    updated = 0

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()

        for s in scored:
            db_id = s.get("db_id")
            if db_id is None:
                continue
            total = s.get("total", 0)
            if s.get("resolved", False):
                continue
            cur.execute(
                "UPDATE curiosities SET score=?, score_updated_at=?, combined_score=? WHERE id=?",
                (total, now, total, db_id),
            )
            if cur.rowcount > 0:
                updated += 1

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error writing scores to DB: {e}", file=sys.stderr)
        return 0

    return updated


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Curiosity Queue Priority Scorer")
    parser.add_argument("--top", type=int, help="Show only top N items")
    parser.add_argument("--thread", type=str, help="Filter by thread name")
    parser.add_argument("--json", action="store_true", help="JSON output for Director")
    parser.add_argument("--report", action="store_true", help="Full report with recommendations")
    parser.add_argument("--diversify", action="store_true", help="Inject orthogonal questions if queue is homogeneous")
    parser.add_argument("--dry-run", action="store_true", help="Show what --diversify would inject without modifying")
    parser.add_argument("--apply", action="store_true", help="Write scores back to prometheus.db curiosities table")
    args = parser.parse_args()

    if args.diversify:
        result = diversify_queue(dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return

    state = load_state()
    scored = score_all(state)

    if args.apply:
        applied = apply_scores(scored)
        print(f"Applied scores: {applied} curiosities updated in prometheus.db")
        if args.json:
            print(json.dumps(scored, indent=2))
        return

    if args.thread:
        scored = [s for s in scored if s["thread"] == args.thread]

    if args.top:
        scored = scored[: args.top]

    if args.json:
        print(json.dumps(scored, indent=2))
    elif args.report:
        print_report(score_all(state))  # Full report always shows everything
    else:
        # Default: compact table
        active = [s for s in scored if not s["resolved"]]
        print(f"{'#':>3} {'Score':>5} {'Thread':<15} {'Text'}")
        print("-" * 80)
        for i, s in enumerate(active):
            print(f"{i+1:3d} {s['total']:5d} {s['thread']:<15} {s['text'][:55]}")


if __name__ == "__main__":
    main()
