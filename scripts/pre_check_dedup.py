#!/usr/bin/env python3
"""
Pre-creation duplicate check — run BEFORE creating any task (batch or manual).
Usage:
  python3 pre_check_dedup.py "hypothesis text here"
  echo "hypothesis text" | python3 pre_check_dedup.py --stdin
  python3 pre_check_dedup.py --batch /path/to/file  (one hypothesis per line)

Returns exit code 0 if CLEAN (safe to create), 1 if DUPLICATE found.
Output is JSON with details.

Thresholds: Jaccard >0.85 (hypothesis/result), dynamic running (0.60-0.75), phrase >0.70, RAG >0.95.
Raised from 0.65/0.45/0.90 to allow re-exploration of stranded findings (June 8 2026).

Layer 3 (embedding cosine): >0.4 catches paraphrases that Jaccard misses.
(exp_72526573 — validated with Jaccard=0.056, cosine=0.654 for paraphrase pairs)
"""

import re
import sys
import json
import sqlite3
import os
import time
import hashlib
from collections import Counter

# --- Constants (must match batch_create_tasks.py) ---

"""CLI tool: Pre Check Dedup.

Usage: python3 pre_check_dedup.py [options]
"""

JACCARD_HYP_THRESHOLD = 0.85
JACCARD_RESULT_THRESHOLD = 0.85
JACCARD_RUNNING_THRESHOLD = 0.60  # Default; overridden by --free-workers flag
PHRASE_OVERLAP_THRESHOLD = 0.70  # Raised from 0.45 — 14K experiments, allow re-exploration
PHRASE_MIN_COUNT = 2

# Layer 3: Embedding cosine similarity threshold
EMBEDDING_COSINE_THRESHOLD = 0.55  # Catches true paraphrases (0.567), rejects related-but-different (0.516)
EMBEDDING_CACHE_DIR = os.path.expanduser("~/.hermes/cache/embeddings")
EMBEDDING_CACHE_TTL = 3600  # Rebuild cache after 1 hour


def get_dynamic_running_threshold(free_count):
    """Scale running-task overlap threshold with worker availability.
    Must match batch_create_tasks.py."""
    if free_count > 20:
        return 0.75
    elif free_count > 5:
        return 0.65
    else:
        return 0.60

STOPWORDS = {
    "does", "that", "this", "with", "from", "have", "been", "will",
    "more", "than", "when", "what", "also", "only", "other",
    "into", "over", "such", "after", "before", "about", "would", "could",
    "should", "might", "being", "there", "their", "which", "these", "those",
    "some", "most", "each", "very", "just", "then",
}


def extract_key_phrases(text):
    """Extract distinctive 2-word phrases from text."""
    words = re.findall(r'\w{3,}', text.lower())
    phrases = set()
    for i in range(len(words) - 1):
        if words[i] not in STOPWORDS and words[i + 1] not in STOPWORDS:
            phrases.add(f"{words[i]} {words[i + 1]}")
    return phrases


def load_completed_experiments():
    """Load completed experiments from prometheus.db."""
    db_path = os.path.expanduser("~/.hermes/prometheus.db")
    completed_hyps = {}
    completed_results = {}
    completed_types = {}  # exp_id -> experiment_type (for [TRANSFER] dedup exemption)
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=3000")
        rows = db.execute(
            "SELECT id, hypothesis, result, experiment_type FROM experiments WHERE status='completed'"
        ).fetchall()
        db.close()
        for eid, hyp, res, etype in rows:
            if hyp and len(hyp) > 20:
                completed_hyps[eid] = hyp.lower()
            if res and len(res) > 20:
                completed_results[eid] = res.lower()[:500]
            if etype:
                completed_types[eid] = etype
    except Exception as e:
        print(json.dumps({"error": f"Failed to load experiments: {e}"}))
        sys.exit(2)
    return completed_hyps, completed_results, completed_types


def load_running_titles(exclude_title=None):
    """Load running task titles from kanban.db, optionally excluding one title."""
    db_path = os.path.expanduser("~/.hermes/kanban.db")
    titles = []
    try:
        db = sqlite3.connect(db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=3000")
        rows = db.execute(
            "SELECT title FROM tasks WHERE status='running'"
        ).fetchall()
        db.close()
        for (t,) in rows:
            if t and t != exclude_title:
                titles.append(t)
    except Exception:
        pass
    return titles


def build_phrase_index(completed_hyps):
    """Build inverted index: phrase -> [exp_ids]."""
    idx = {}
    for exp_id, hyp in completed_hyps.items():
        for p in extract_key_phrases(hyp):
            if p not in idx:
                idx[p] = []
            idx[p].append(exp_id)
    return idx


# --- Layer 3: Embedding-based conceptual similarity ---

class EmbeddingProvider:
    """Embedding provider with graceful degradation.

    Tries the Qwen3 embedding server (:9150) first, falls back to local sentence-transformers.
    Lazy-loaded — no cost until first use.
    """
    _model = None
    _use_server = None  # None = untested, True = server, False = local

    @classmethod
    def _try_server(cls, texts):
        """Try the Qwen3 embedding server (:9150, llama.cpp)."""
        try:
            import requests
            resp = requests.post(
                "http://localhost:9150/embedding",
                json={"input": texts},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                # llama.cpp: list of {"embedding": [...]} (maybe nested [[...]]), or {"embeddings": [...]}
                if isinstance(data, list):
                    out = []
                    for item in data:
                        emb = item["embedding"] if isinstance(item, dict) else item
                        if emb and isinstance(emb[0], list):
                            emb = emb[0]
                        out.append(emb)
                    return out
                if isinstance(data, dict) and "embeddings" in data:
                    return data["embeddings"]
        except Exception:
            pass
        return None

    @classmethod
    def _try_local(cls, texts):
        """Use local sentence-transformers model."""
        if cls._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                cls._model = SentenceTransformer('all-MiniLM-L6-v2')
            except ImportError:
                return None
        embeddings = cls._model.encode(texts, batch_size=16, show_progress_bar=False)
        return embeddings.tolist()

    @classmethod
    def encode(cls, texts):
        """Encode texts to embeddings. Returns list of vectors."""
        if cls._use_server is True:
            result = cls._try_server(texts)
            if result is not None:
                return result
            cls._use_server = False

        if cls._use_server is False:
            result = cls._try_local(texts)
            if result is not None:
                return result
            return None

        # First call: try server first
        result = cls._try_server(texts)
        if result is not None:
            cls._use_server = True
            return result

        cls._use_server = False
        result = cls._try_local(texts)
        return result


def cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embedding_cache_path():
    """Path to embedding index cache file."""
    os.makedirs(EMBEDDING_CACHE_DIR, exist_ok=True)
    return os.path.join(EMBEDDING_CACHE_DIR, "experiment_embeddings.json")


def _cache_is_fresh(path):
    """Check if cache file is less than EMBEDDING_CACHE_TTL seconds old."""
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < EMBEDDING_CACHE_TTL


def _load_embedding_cache():
    """Load cached embedding index. Returns (exp_ids, embeddings) or (None, None)."""
    path = _embedding_cache_path()
    if not _cache_is_fresh(path):
        return None, None
    try:
        with open(path) as f:
            data = json.load(f)
        return data["exp_ids"], data["embeddings"]
    except Exception:
        return None, None


def _save_embedding_cache(exp_ids, embeddings):
    """Save embedding index to cache."""
    path = _embedding_cache_path()
    try:
        with open(path, 'w') as f:
            json.dump({"exp_ids": exp_ids, "embeddings": embeddings})
    except Exception:
        pass


def _build_embedding_index(completed_hyps, completed_types, is_transfer):
    """Build embedding index for all eligible experiments.

    Returns (exp_ids, embeddings) where embeddings[i] corresponds to exp_ids[i].
    """
    # Filter candidates using same [TRANSFER] exemption logic
    candidates = {}
    for exp_id, hyp in completed_hyps.items():
        if is_transfer and completed_types and completed_types.get(exp_id) != "ANALOGICAL":
            continue
        candidates[exp_id] = hyp

    if not candidates:
        return [], []

    exp_ids = list(candidates.keys())
    texts = [candidates[eid] for eid in exp_ids]

    # Batch encode all texts
    embeddings = EmbeddingProvider.encode(texts)
    if embeddings is None:
        return [], []

    return exp_ids, embeddings


def embedding_layer3(hypothesis, completed_hyps, completed_types, is_transfer, is_stranded):
    """Layer 3: Embedding-based conceptual similarity gate.

    Uses a cached embedding index for fast lookup. Only encodes the new
    hypothesis and compares against pre-computed embeddings.

    Returns: list of matches [{exp_id, method, score}]
    """
    if is_stranded:
        return []

    matches = []

    # Try loading from cache
    cached_ids, cached_embs = _load_embedding_cache()

    if cached_ids is None or cached_embs is None:
        # Build fresh index
        cached_ids, cached_embs = _build_embedding_index(
            completed_hyps, completed_types, is_transfer
        )
        if cached_ids:
            _save_embedding_cache(cached_ids, cached_embs)

    if not cached_ids:
        return []

    # Encode just the new hypothesis
    hyp_emb = EmbeddingProvider.encode([hypothesis.lower()])
    if hyp_emb is None or len(hyp_emb) == 0:
        return []
    hyp_emb = hyp_emb[0]

    # Compare against all cached embeddings
    for i, exp_id in enumerate(cached_ids):
        sim = cosine_similarity(hyp_emb, cached_embs[i])
        if sim > EMBEDDING_COSINE_THRESHOLD:
            matches.append({
                "exp_id": exp_id,
                "method": "embedding_cosine",
                "score": round(sim, 3)
            })

    return matches


def check_duplicate(hypothesis, completed_hyps, completed_results, running_titles, phrase_index, running_threshold=None, completed_types=None):
    """
    Check if hypothesis is a duplicate. Returns dict with:
      is_duplicate: bool
      matches: list of {exp_id, method, score}

    [TRANSFER] dedup exemption: cross-domain hypotheses share vocabulary with
    source experiments by design. Skip word-overlap dedup against non-[TRANSFER]
    completed experiments. Still check against:
      - Running tasks (active duplicates)
      - [TRANSFER] completed experiments (same-class duplicates)
    """
    if running_threshold is None:
        running_threshold = JACCARD_RUNNING_THRESHOLD
    hyp_lower = hypothesis.lower()
    hyp_words = set(re.findall(r'\w{4,}', hyp_lower))
    hyp_phrases = extract_key_phrases(hyp_lower)
    matches = []

    is_transfer = "[transfer" in hyp_lower or "[analogical]" in hyp_lower
    is_stranded = "[stranded" in hyp_lower

    if not hyp_words:
        return {"is_duplicate": False, "matches": []}

    # Layer 1: Jaccard against running tasks (exclude self-match)
    # ALWAYS check — running tasks are active duplicates regardless of type
    for rt in running_titles:
        rt_words = set(re.findall(r'\w{4,}', rt.lower()))
        if rt_words:
            overlap = len(hyp_words & rt_words) / max(len(hyp_words | rt_words), 1)
            if overlap > running_threshold:
                matches.append({
                    "exp_id": f"running: {rt[:80]}",
                    "method": "jaccard_running",
                    "score": round(overlap, 3)
                })

    # Layer 1b: Jaccard against completed hypotheses
    for exp_id, hyp in completed_hyps.items():
        if is_stranded:
            continue
        if is_transfer and completed_types and completed_types.get(exp_id) != "ANALOGICAL":
            continue
        hyp2_words = set(re.findall(r'\w{4,}', hyp))
        if hyp2_words:
            overlap = len(hyp_words & hyp2_words) / max(len(hyp_words | hyp2_words), 1)
            if overlap > JACCARD_HYP_THRESHOLD:
                matches.append({
                    "exp_id": exp_id,
                    "method": "jaccard_hypothesis",
                    "score": round(overlap, 3)
                })

    # Layer 1: Jaccard against completed results
    for exp_id, res in completed_results.items():
        if is_stranded:
            continue
        if is_transfer and completed_types and completed_types.get(exp_id) != "ANALOGICAL":
            continue
        res_words = set(re.findall(r'\w{4,}', res))
        if res_words:
            overlap = len(hyp_words & res_words) / max(len(hyp_words | res_words), 1)
            if overlap > JACCARD_RESULT_THRESHOLD:
                matches.append({
                    "exp_id": exp_id,
                    "method": "jaccard_result",
                    "score": round(overlap, 3)
                })

    # Layer 2: Phrase-level overlap
    if hyp_phrases:
        candidate_counts = Counter()
        for phrase in hyp_phrases:
            if phrase in phrase_index:
                for exp_id in phrase_index[phrase]:
                    candidate_counts[exp_id] += 1
        for exp_id, count in candidate_counts.most_common(10):
            if count >= PHRASE_MIN_COUNT and exp_id in completed_hyps:
                if is_stranded:
                    continue
                if is_transfer and completed_types and completed_types.get(exp_id) != "ANALOGICAL":
                    continue
                ratio = count / max(len(hyp_phrases), 1)
                if ratio > PHRASE_OVERLAP_THRESHOLD:
                    matches.append({
                        "exp_id": exp_id,
                        "method": "phrase_overlap",
                        "score": round(ratio, 3),
                        "phrases_matched": count,
                        "total_phrases": len(hyp_phrases)
                    })

    # Layer 3: Embedding cosine similarity (only if Layer 1+2 found nothing)
    layer12_matches = [m for m in matches if m["method"] != "embedding_cosine"]
    use_embedding = not getattr(check_duplicate, '_no_embedding', False)
    if not layer12_matches and use_embedding:
        emb_matches = embedding_layer3(
            hypothesis, completed_hyps, completed_types, is_transfer, is_stranded
        )
        # [TRANSFER] exemption: cross-domain transfers share vocabulary with
        # source experiments by design. Embedding cosine catches them because
        # the semantic space overlaps, but they're genuinely different questions.
        # Skip ALL embedding matches for transfer items — consistent with
        # batch_create_tasks.py which skips pre_check entirely for ANALOGICAL.
        if is_transfer:
            emb_matches = []
        matches.extend(emb_matches)

    # Deduplicate matches by exp_id (keep best score)
    best = {}
    for m in matches:
        eid = m["exp_id"]
        if eid not in best or m["score"] > best[eid]["score"]:
            best[eid] = m

    return {
        "is_duplicate": len(best) > 0,
        "matches": sorted(best.values(), key=lambda x: -x["score"])
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pre-creation duplicate check")
    parser.add_argument("hypothesis", nargs="?", help="Hypothesis text to check")
    parser.add_argument("--stdin", action="store_true", help="Read from stdin")
    parser.add_argument("--batch", help="File with one hypothesis per line")
    parser.add_argument("--free-workers", type=int, default=None,
                        help="Number of free workers (adjusts running-task threshold dynamically)")
    parser.add_argument("--verbose", action="store_true", help="Show loaded counts")
    parser.add_argument("--no-embedding", action="store_true",
                        help="Skip Layer 3 embedding check (faster, less accurate)")
    args = parser.parse_args()

    # Load data once
    completed_hyps, completed_results, completed_types = load_completed_experiments()
    phrase_index = build_phrase_index(completed_hyps)

    if args.verbose:
        print(json.dumps({
            "loaded": {
                "completed_hyps": len(completed_hyps),
                "completed_results": len(completed_results),
                "phrase_index_entries": len(phrase_index)
            }
        }), file=sys.stderr)

    hypotheses = []

    if args.batch:
        with open(args.batch) as f:
            hypotheses = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    elif args.stdin:
        hypotheses = [line.strip() for line in sys.stdin if line.strip()]
    elif args.hypothesis:
        hypotheses = [args.hypothesis]
    else:
        parser.print_help()
        sys.exit(2)

    results = []
    any_dup = False

    # Compute dynamic running threshold from --free-workers flag
    if args.free_workers is not None:
        running_threshold = get_dynamic_running_threshold(args.free_workers)
    else:
        running_threshold = JACCARD_RUNNING_THRESHOLD  # default (0.60)

    for hyp in hypotheses:
        check_duplicate._no_embedding = args.no_embedding
        # Load running titles fresh for each check (exclude the hypothesis itself)
        running_titles = load_running_titles(exclude_title=hyp)
        result = check_duplicate(hyp, completed_hyps, completed_results, running_titles, phrase_index, running_threshold=running_threshold, completed_types=completed_types)
        result["hypothesis"] = hyp[:200]
        results.append(result)
        if result["is_duplicate"]:
            any_dup = True

    if len(results) == 1:
        output = results[0]
    else:
        output = {"results": results, "any_duplicate": any_dup}

    print(json.dumps(output, indent=2))
    sys.exit(1 if any_dup else 0)


if __name__ == "__main__":
    main()
