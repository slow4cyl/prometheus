#!/usr/bin/env python3
"""
faiss_dedup.py — Persistent FAISS index for fast batch dedup.

Rebuilds the experiment embedding index once, caches it to disk,
and provides O(1)-per-query batch dedup via cosine similarity.

Usage:
    from faiss_dedup import get_index, check_duplicates_batch

    index = get_index()  # loads from cache or builds (~30s first time)
    is_dup = check_duplicates_batch(["hypothesis 1", "hypothesis 2"], index)
"""
import json
from prometheus_paths import HERMES_HOME as _PP_HERMES_HOME
import os as _os
_os.environ.setdefault('OMP_THREAD_LIMIT', '1')
_os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
_os.environ.setdefault('MKL_NUM_THREADS', '1')
import os
import sqlite3
import time
import numpy as np
import faiss
from pathlib import Path
from typing import List, Tuple

HERMES = Path(_PP_HERMES_HOME)
MAIN_HERMES = Path(os.path.expanduser("~/.hermes"))
RAG_DIR = HERMES / "rag"
INDEX_PATH = RAG_DIR / "faiss_index.faiss"
META_PATH = RAG_DIR / "faiss_index.meta.json"

def _resolve_prometheus_db() -> Path:
    """Resolve prometheus.db — prefer HERMES_HOME, fall back to main directory.

    When running under a worker profile (HERMES_HOME points to a profile
    directory with an empty/missing prometheus.db), fall back to the main
    hermes directory which has the real database.  rag.db is hardlinked
    across profiles so it doesn't need this treatment.
    """
    for candidate in (HERMES / "prometheus.db", MAIN_HERMES / "prometheus.db"):
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return HERMES / "prometheus.db"  # let the caller get the real error

PROMETHEUS_DB = _resolve_prometheus_db()
EMBEDDING_PORT = 9150
EMBED_MODEL = "qwen3-embedding-0.6b"
EMBED_BATCH_SIZE = 128  # texts per embedding server request


def _embed_batch(texts: List[str], prefix: str = "") -> np.ndarray:
    """Batch-embed texts via local Qwen3-Embedding-0.6B server. Returns L2-normalized float32.

    Retries on HTTP 500 (transient server overload) with exponential backoff
    and automatic batch halving — the llama.cpp embedding server can return
    500 when processing many rapid-fire batches.
    """
    import urllib.request
    import urllib.error

    # Truncate to avoid llama.cpp embedding server 500 errors on long texts
    # The Qwen3-Embedding-0.6B server via llama.cpp returns HTTP 500 on input > ~1890
    # chars (including the "search_document:" prefix).  Use 1800 for safety.
    texts = [t[:1800] if len(t) > 1800 else t for t in texts]
    prefixed = [f"{prefix}{t}" for t in texts]
    all_embeddings = []

    batch_size = EMBED_BATCH_SIZE
    i = 0
    while i < len(prefixed):
        chunk = prefixed[i: i + batch_size]
        payload = json.dumps({"input": chunk}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{EMBEDDING_PORT}/embedding",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 500 and batch_size > 8:
                batch_size = max(8, batch_size // 2)
                print(f"  HTTP 500 at batch {i}, halving batch to {batch_size}", flush=True)
                time.sleep(2)
                continue
            raise

        # Small delay between batches to avoid overwhelming the embedding server
        if i > 0 and i % (batch_size * 10) == 0:
            time.sleep(0.5)

        # Parse response (same format as experiment_rag.py)
        if isinstance(data, list) and len(data) > 0:
            if isinstance(data[0], dict) and "embedding" in data[0]:
                embeddings = [
                    item["embedding"][0] if isinstance(item["embedding"][0], list)
                    else item["embedding"]
                    for item in data
                ]
            else:
                embeddings = data
        else:
            embeddings = data

        all_embeddings.extend(embeddings)
        i += len(chunk)

    arr = np.array(all_embeddings, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr.reshape(arr.shape[0], -1)
    # L2-normalize for cosine similarity via inner product
    faiss.normalize_L2(arr)
    return arr


def _get_experiment_hypotheses() -> List[Tuple[str, str]]:
    """Get (exp_id, hypothesis) pairs from prometheus.db."""
    conn = sqlite3.connect(str(PROMETHEUS_DB), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    rows = conn.execute(
        "SELECT id, hypothesis FROM experiments "
        "WHERE hypothesis IS NOT NULL AND hypothesis != '' "
        "ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def _get_last_exp_id() -> int:
    """Get the numeric part of the last experiment ID for incremental updates."""
    conn = sqlite3.connect(str(PROMETHEUS_DB), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    row = conn.execute(
        "SELECT id FROM experiments ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row and row[0]:
        import re
        m = re.search(r"(\d+)", row[0])
        return int(m.group(1)) if m else 0
    return 0


def build_index() -> Tuple[faiss.Index, List[str]]:
    """Build FAISS index from all experiments. Returns (index, exp_ids)."""
    print("Building FAISS index from experiments...", flush=True)
    t0 = time.time()

    experiments = _get_experiment_hypotheses()
    print(f"  {len(experiments)} experiments to embed", flush=True)

    exp_ids = [e[0] for e in experiments]
    hypotheses = [e[1] for e in experiments]

    # Truncate long hypotheses to avoid llama.cpp embedding server 500 errors
    # (Qwen3-Embedding-0.6B has 8192 token context, but llama.cpp Q4_K_M returns 500 on >~2450 chars)
    hypotheses = [h[:2000] if len(h) > 2000 else h for h in hypotheses]

    # Embed all hypotheses in batches
    embeddings = _embed_batch(hypotheses, prefix="")
    print(f"  Embedded in {time.time()-t0:.1f}s", flush=True)

    # Build FAISS index (exact cosine via inner product on normalized vectors)
    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)

    # Cache to disk
    RAG_DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(INDEX_PATH))
    meta = {
        "count": len(exp_ids),
        "dim": dim,
        "exp_ids": exp_ids,
        "built_at": time.time(),
    }
    META_PATH.write_text(json.dumps(meta))

    size_mb = os.path.getsize(str(INDEX_PATH)) / 1e6
    print(f"  Index built: {index.ntotal} vectors, {dim}d, {size_mb:.1f}MB, {time.time()-t0:.1f}s", flush=True)
    return index, exp_ids


def load_index() -> Tuple[faiss.Index, List[str]]:
    """Load cached FAISS index from disk. Returns (index, exp_ids)."""
    if not INDEX_PATH.exists() or not META_PATH.exists():
        return build_index()

    meta = json.loads(META_PATH.read_text())
    current_count = len(_get_experiment_hypotheses())

    # Rebuild if count changed by more than 5% (tolerate slight staleness)
    if abs(meta["count"] - current_count) > max(50, meta["count"] * 0.05):
        print(f"Index stale ({meta['count']} vs {current_count} experiments), rebuilding...", flush=True)
        return build_index()

    index = faiss.read_index(str(INDEX_PATH))
    print(f"Loaded FAISS index from cache: {index.ntotal} vectors", flush=True)
    return index, meta.get("exp_ids", [])


def get_index() -> Tuple[faiss.Index, List[str]]:
    """Get FAISS index — load from cache or build."""
    return load_index()


def check_duplicates_batch(
    texts: List[str],
    index: faiss.Index,
    exp_ids: List[str],
    threshold: float = 0.95,
) -> List[dict]:
    """Batch check for duplicates. Returns list of dicts with is_dup, score, match_id."""
    if not texts:
        return []

    t0 = time.time()
    embeddings = _embed_batch(texts, prefix="Instruct: Retrieve prior experiments with a semantically equivalent hypothesis\nQuery: ")
    distances, indices = index.search(embeddings, k=1)

    results = []
    for i, (dist, idx) in enumerate(zip(distances, indices)):
        score = float(dist[0])
        match_id = exp_ids[idx[0]] if idx[0] < len(exp_ids) else None
        results.append({
            "is_dup": score >= threshold,
            "score": score,
            "match_id": match_id,
        })

    elapsed = time.time() - t0
    dup_count = sum(1 for r in results if r["is_dup"])
    print(f"FAISS dedup: {len(texts)} texts, {dup_count} duplicates, {elapsed:.2f}s", flush=True)
    return results


if __name__ == "__main__":
    # CLI: build or query
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_index()
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        if INDEX_PATH.exists() and META_PATH.exists():
            meta = json.loads(META_PATH.read_text())
            print(f"Index: {meta['count']} experiments, {meta['dim']}d")
            print(f"Built: {time.strftime('%Y-%m-%d %H:%M', time.localtime(meta['built_at']))}")
            print(f"Size: {os.path.getsize(str(INDEX_PATH))/1e6:.1f}MB")
        else:
            print("No index cached yet")
    else:
        print("Usage: faiss_dedup.py [build|status]")
