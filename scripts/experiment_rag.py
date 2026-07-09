#!/usr/bin/env python3
"""
experiment_rag — Local RAG system for Prometheus experiment results.

Maintains a permanent embedding index of all experiments, session highlights,
and knowledge artifacts. Workers query it to find related past work before
starting new experiments.

Architecture:
  - Embedding model (Qwen3-Embedding-0.6B) runs permanently on GPU (~300MB VRAM)
  - Embeddings stored in numpy arrays + SQLite metadata
  - Incremental indexing: only processes new/changed files
  - CLI interface for workers: query, index, status

Usage:
  python3 experiment_rag.py index              # Index all experiments
  python3 experiment_rag.py index --incremental # Only new/changed
  python3 experiment_rag.py index --reindex-failed  # Retry previously failed
  python3 experiment_rag.py query "quantization" --top-k 5
  python3 experiment_rag.py status
  python3 experiment_rag.py serve              # Keep embedding server alive
"""

import os
import sys
import re
import json
import time
import hashlib
import sqlite3
import resource

# Increase file descriptor limit for large-scale indexing
# Default 1024 is too low for 79K+ experiment embedding files
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 65536:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(65536, hard), hard))
except (ValueError, resource.error):
    pass  # Non-linux or unprivileged, proceed with default limit
import subprocess
import signal
import logging
import numpy as np
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime

# ── Paths ──

"""CLI tool: Experiment Rag.

Usage: python3 experiment_rag.py [options]
"""

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
_MAIN_HERMES = Path(os.path.expanduser("~/.hermes"))
RAG_DIR = HERMES_HOME / "rag"
RAG_DB = RAG_DIR / "rag.db"
EMBEDDINGS_DIR = RAG_DIR / "embeddings"
EXPERIMENTS_DIR = HERMES_HOME / "experiments"

def _resolve_prometheus_db() -> Path:
    """Resolve prometheus.db — prefer HERMES_HOME, fall back to main directory.

    Worker profiles have an empty/missing prometheus.db.  rag.db is
    hardlinked across profiles so it doesn't need this treatment, but
    prometheus.db is not.
    """
    for candidate in (HERMES_HOME / "prometheus.db", _MAIN_HERMES / "prometheus.db"):
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return HERMES_HOME / "prometheus.db"  # let caller get the real error

for d in [RAG_DIR, EMBEDDINGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("experiment_rag")

# ── Binary file detection ──
SKIP_EXTENSIONS = {".pyc", ".pyo", ".so", ".o", ".bin", ".dat", ".pkl", ".npy", ".npz"}
MAX_FILE_SIZE = 1_000_000  # 1MB — skip files larger than this


def is_binary_file(fpath: Path) -> bool:
    """Detect binary files by extension, size, and null bytes."""
    if fpath.suffix.lower() in SKIP_EXTENSIONS:
        return True
    try:
        if fpath.stat().st_size > MAX_FILE_SIZE:
            return True
        # Check first 1KB for null bytes
        with open(fpath, "rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                return True
    except Exception:
        return True
    return False


# ── Text sanitization ──

def sanitize_for_embedding(text: str) -> str:
    """
    Clean text for safe embedding. Surgical — only removes what causes 500 errors.
    
    Preserves: unicode, math symbols, code, most whitespace
    Removes: null bytes, control chars, extremely long lines
    """
    # Remove null bytes and control characters (except \n \t \r)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Encode/decode to fix broken unicode
    text = text.encode("utf-8", errors="ignore").decode("utf-8")
    # Collapse excessive whitespace (3+ newlines -> 2)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Remove lines longer than 500 chars (likely code/binary)
    lines = text.split("\n")
    lines = [l for l in lines if len(l) < 500]
    text = "\n".join(lines)
    return text


def extract_json_fields(content: str) -> Optional[str]:
    """
    Try to extract meaningful fields from JSON experiment files.
    
    Falls back to None if content isn't parseable JSON.
    """
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        
        # Extract key experiment fields
        parts = []
        for field in ["title", "hypothesis", "result", "domain", "description", "summary"]:
            if field in data and data[field]:
                parts.append(f"{field.title()}: {data[field]}")
        
        # Also include tags if present
        if "tags" in data and isinstance(data["tags"], list):
            parts.append(f"Tags: {', '.join(str(t) for t in data['tags'][:5])}")
        
        if parts:
            return "\n".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def prepare_text_for_embedding(content: str, fpath: Path) -> str:
    """
    Prepare file content for embedding. Tries smart extraction first,
    falls back to sanitized truncation.
    """
    # For JSON files, try to extract key fields
    if fpath.suffix.lower() == ".json":
        extracted = extract_json_fields(content)
        if extracted and len(extracted) > 50:
            return sanitize_for_embedding(extracted)
    
    # For code files, try to extract docstrings/comments
    if fpath.suffix.lower() == ".py":
        # Look for module docstring
        match = re.search(r'"""(.+?)"""', content, re.DOTALL)
        if match and len(match.group(1)) > 50:
            return sanitize_for_embedding(match.group(1)[:1000])
        # Look for comments at top
        lines = content.split("\n")
        comment_lines = [l.strip("# ") for l in lines[:20] if l.strip().startswith("#")]
        if comment_lines:
            return sanitize_for_embedding("\n".join(comment_lines)[:1000])
    
    # Default: sanitize and truncate
    return sanitize_for_embedding(content[:1000])

# ── Title Extraction ──

def extract_title(content: str, fpath: Path) -> str:
    """
    Extract a meaningful title from file content for RAG indexing.
    
    Falls back to filename stem if no structured title found.
    Workers see this in query results — generic titles like "exp_2818_results"
    make it impossible to triage which results are worth reading.
    """
    stem = fpath.stem  # fallback
    
    if fpath.suffix.lower() == ".json":
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                # Prefer "title" field, then "hypothesis", then "experiment" + domain
                title = data.get("title")
                if title and len(str(title)) > 10:
                    return str(title)[:100]
                hyp = data.get("hypothesis")
                if hyp and len(str(hyp)) > 10:
                    return str(hyp)[:100]
                # Compound: "exp_NNN: domain"
                exp_id = data.get("experiment", "")
                domain = data.get("domain", "")
                if exp_id and domain:
                    return f"{exp_id}: {domain}"[:100]
        except (json.JSONDecodeError, TypeError):
            pass
    
    if fpath.suffix.lower() == ".md":
        # First # heading
        match = re.search(r'^#\s+(.+)', content, re.MULTILINE)
        if match:
            return match.group(1).strip()[:100]
        # First non-empty line
        for line in content.split("\n"):
            line = line.strip()
            if line and len(line) > 10:
                return line[:100]
    
    if fpath.suffix.lower() == ".py":
        # Module docstring
        match = re.search(r'"""(.+?)"""', content, re.DOTALL)
        if match and len(match.group(1).strip()) > 10:
            return match.group(1).strip().split("\n")[0][:100]
        # First comment
        for line in content.split("\n")[:20]:
            line = line.strip()
            if line.startswith("#") and len(line) > 10:
                return line.lstrip("# ").strip()[:100]
    
    if fpath.suffix.lower() in (".txt", ".jsonl"):
        for line in content.split("\n")[:10]:
            line = line.strip()
            if line and len(line) > 10:
                return line[:100]
    
    # Fallback: try first line of any file
    for line in content.split("\n")[:5]:
        line = line.strip()
        if line and len(line) > 15 and not line.startswith("{"):
            return line[:100]
    
    return stem


def extract_preview(content: str, fpath: Path) -> str:
    """
    Extract a worker-readable preview from file content for RAG query results.
    
    Workers see this when they query RAG — it's the "should I read this?" signal.
    Unlike extract_title() (one line), this produces a 2-3 line summary.
    """
    MAX_LEN = 200
    
    if fpath.suffix.lower() == ".json":
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                parts = []
                # Hypothesis first — most important for triage
                hyp = data.get("hypothesis")
                if hyp and len(str(hyp)) > 10:
                    parts.append(f"Hypothesis: {str(hyp)[:100]}")
                # Result second
                res = data.get("result")
                if res and len(str(res)) > 10:
                    parts.append(f"Result: {str(res)[:80]}")
                # Domain if present
                domain = data.get("domain")
                if domain:
                    parts.append(f"Domain: {domain}")
                if parts:
                    return "\n".join(parts)[:MAX_LEN]
        except (json.JSONDecodeError, TypeError):
            pass
    
    if fpath.suffix.lower() == ".md":
        lines = content.split("\n")
        summary_lines = []
        for line in lines[:30]:
            stripped = line.strip()
            if not stripped:
                continue
            # Skip markdown formatting markers
            if stripped.startswith("---") or stripped.startswith("```"):
                continue
            summary_lines.append(stripped.lstrip("# ").strip())
            if len("\n".join(summary_lines)) > MAX_LEN:
                break
        if summary_lines:
            return "\n".join(summary_lines)[:MAX_LEN]
    
    if fpath.suffix.lower() == ".py":
        # Docstring or first meaningful comments
        match = re.search(r'"""(.+?)"""', content, re.DOTALL)
        if match and len(match.group(1).strip()) > 10:
            return match.group(1).strip()[:MAX_LEN]
        lines = []
        for line in content.split("\n")[:20]:
            stripped = line.strip()
            if stripped.startswith("#") and len(stripped) > 10:
                lines.append(stripped.lstrip("# ").strip())
                if len("\n".join(lines)) > MAX_LEN:
                    break
        if lines:
            return "\n".join(lines)[:MAX_LEN]
    
    if fpath.suffix.lower() in (".txt", ".jsonl"):
        lines = []
        for line in content.split("\n")[:10]:
            stripped = line.strip()
            if stripped and len(stripped) > 5:
                lines.append(stripped)
                if len("\n".join(lines)) > MAX_LEN:
                    break
        if lines:
            return "\n".join(lines)[:MAX_LEN]
    
    # Fallback: first meaningful lines
    lines = []
    for line in content.split("\n")[:10]:
        stripped = line.strip()
        if stripped and len(stripped) > 10 and not stripped.startswith("{"):
            lines.append(stripped)
            if len("\n".join(lines)) > MAX_LEN:
                break
    return "\n".join(lines)[:MAX_LEN] if lines else content[:MAX_LEN]


# ── Database ──

def get_db():
    conn = sqlite3.connect(str(RAG_DB), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # crash-safe; OFF corrupts on SIGKILL/OOM
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_type TEXT NOT NULL,
                source_path TEXT,
                title TEXT,
                content_hash TEXT,
                content_preview TEXT,
                embedding_file TEXT,
                created_at REAL NOT NULL,
                indexed_at REAL,
                embedding_dim INTEGER
            );

            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY,
                title TEXT,
                hypothesis TEXT,
                result TEXT,
                domain TEXT,
                tags TEXT,
                status TEXT,
                created_at REAL,
                indexed_at REAL,
                embedding_file TEXT,
                embedding_dim INTEGER
            );

            CREATE TABLE IF NOT EXISTS session_highlights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                message_id INTEGER,
                role TEXT,
                content_preview TEXT,
                topic TEXT,
                created_at REAL,
                indexed_at REAL
            );

            CREATE TABLE IF NOT EXISTS index_stats (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at REAL
            );

            CREATE TABLE IF NOT EXISTS failed_files (
                path TEXT PRIMARY KEY,
                error TEXT,
                attempts INTEGER DEFAULT 0,
                last_attempt REAL
            );

            CREATE TABLE IF NOT EXISTS query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_text TEXT,
                worker_id TEXT,
                result_count INTEGER,
                avg_score REAL,
                top_score REAL,
                top_result_title TEXT,
                queried_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_docs_type ON documents(doc_type);
            CREATE INDEX IF NOT EXISTS idx_docs_hash ON documents(content_hash);
            CREATE INDEX IF NOT EXISTS idx_exp_domain ON experiments(domain);
            CREATE INDEX IF NOT EXISTS idx_query_log_at ON query_log(queried_at);
        """)

        # Migration for pre-existing DBs: CREATE TABLE IF NOT EXISTS will not add
        # new columns to a table that already exists. Add the embedding columns
        # idempotently. (ROOT-CAUSE FIX 2026-06-08: experiment embeddings were
        # previously computed and discarded; these columns let us persist them.)
        for _col, _type in (("embedding_file", "TEXT"), ("embedding_dim", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE experiments ADD COLUMN {_col} {_type}")
            except sqlite3.OperationalError:
                pass  # column already exists


# ── Embedding Server ──

EMBEDDING_PORT = 9150
EMBEDDING_MODEL = "qwen3-embedding-0.6b"
EMBEDDING_VRAM_MB = 1600

_server_process = None


def start_embedding_server() -> str:
    """Start a permanent embedding server on the GPU."""
    global _server_process

    url = f"http://127.0.0.1:{EMBEDDING_PORT}"

    # Check if already running
    try:
        import urllib.request
        req = urllib.request.urlopen(f"{url}/health", timeout=2)
        if req.status == 200:
            logger.info(f"Embedding server already running at {url}")
            return url
    except Exception:
        pass

    # Find the GGUF
    gguf_path = HERMES_HOME / "gpu_models" / "gguf" / "qwen3-embedding-0.6b" / "Qwen3-Embedding-0.6B-Q8_0.gguf"
    if not gguf_path.exists():
        raise RuntimeError(f"Qwen3 embedding GGUF not found at {gguf_path}")

    # Start server
    cmd = [
        "llama-server",
        "-m", str(gguf_path),
        "--port", str(EMBEDDING_PORT),
        "--host", "127.0.0.1",
        "-ngl", "99",
        "--ctx-size", "2048",
        "--no-mmap",
        "--log-disable",
        "--embedding",
        "--pooling", "last",
        "--embd-normalize", "2",
    ]

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = "0"

    _server_process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        preexec_fn=os.setsid,
    )

    # Wait for ready
    import urllib.request
    for _ in range(30):
        try:
            req = urllib.request.urlopen(f"{url}/health", timeout=2)
            if req.status == 200:
                logger.info(f"Embedding server ready at {url} (pid={_server_process.pid})")
                return url
        except Exception:
            pass
        time.sleep(1)

    raise RuntimeError("Embedding server failed to start within 30s")


def get_embeddings(texts: List[str], url: str = None) -> np.ndarray:
    """Get embeddings from the server."""
    if not url:
        url = f"http://127.0.0.1:{EMBEDDING_PORT}"

    # Truncate long texts to avoid llama.cpp 500 errors (>~2400 chars)
    texts = [t[:2000] if len(t) > 2000 else t for t in texts]

    import urllib.request
    payload = json.dumps({"input": texts}).encode()
    req = urllib.request.Request(
        f"{url}/embedding",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    # llama.cpp embedding response formats:
    # Single text: [[embedding]]
    # Batch: [{"index": N, "embedding": [floats]}, ...]
    if isinstance(data, list) and len(data) > 0:
        if isinstance(data[0], dict) and "embedding" in data[0]:
            # Batch: [{"index": N, "embedding": [[floats]]}]
            embeddings = [item["embedding"][0] if isinstance(item["embedding"][0], list) else item["embedding"] for item in data]
        elif isinstance(data[0], list) and len(data[0]) > 0 and isinstance(data[0][0], float):
            embeddings = data
        else:
            embeddings = data
    else:
        embeddings = data
    result = np.array(embeddings, dtype=np.float32)
    # Flatten extra dimensions if needed (e.g. (N, 1, 768) -> (N, 768))
    if result.ndim == 3:
        result = result.reshape(result.shape[0], -1)
    return result


# Embedding batch size. The GPU embed server parallelizes WITHIN a batch — a 256-text
# call runs ~5x the throughput of 32-text calls (measured 204 vs 44 embeds/sec) — while
# CONCURRENT requests to the single GPU serialize AND add overhead (measured 0.4x, i.e.
# slower). So the right parallelism here is a big batch, not threads. Env-overridable;
# get_embeddings_safe halves on any server error, so an over-large value degrades safely.
EMBED_BATCH = int(os.environ.get("RAG_EMBED_BATCH", "256"))


def get_embeddings_safe(texts: List[str], url: str, batch_size: int = EMBED_BATCH) -> Optional[np.ndarray]:
    """
    Get embeddings with error handling. Returns None on failure.
    Tries full batch, then smaller batches, then individual items.
    """
    try:
        return get_embeddings(texts[:batch_size], url)
    except Exception as e:
        logger.warning(f"Batch embedding failed ({len(texts)} items): {e}")
    
    # Try smaller batch
    if batch_size > 8:
        try:
            return get_embeddings(texts[:batch_size // 2], url)
        except Exception:
            pass
    
    # Try individual items, collecting successes
    results = []
    for i, text in enumerate(texts):
        try:
            emb = get_embeddings([text], url)
            results.append(emb[0])
            time.sleep(0.05)  # Rate limit
        except Exception as e:
            logger.warning(f"  Item {i} failed: {e}")
            results.append(None)
    
    if not results or all(r is None for r in results):
        return None
    
    # Return only successful embeddings
    valid = [r for r in results if r is not None]
    return np.array(valid, dtype=np.float32) if valid else None


# ── Indexing ──

def index_experiments(incremental: bool = True, reindex_failed: bool = False, force: bool = False):
    """Index all experiment results into the embedding database."""
    logger.info("Starting experiment indexing...")
    start = time.time()

    init_db()
    url = start_embedding_server()

    # Fast path: check last index time to skip unchanged files by mtime
    last_index_path = os.path.expanduser("~/.hermes/.rag_last_index_time")
    last_index_time = 0
    if os.path.exists(last_index_path):
        try:
            with open(last_index_path) as f:
                last_index_time = float(f.read().strip())
        except:
            pass

    # Ultra-fast path: if directory mtime hasn't changed, skip entirely
    dir_mtime_path = os.path.expanduser("~/.hermes/.rag_dir_mtime")
    if incremental and not force and last_index_time > 0:
        try:
            dir_mtime = EXPERIMENTS_DIR.stat().st_mtime
            if os.path.exists(dir_mtime_path):
                with open(dir_mtime_path) as f:
                    saved_mtime = float(f.read().strip())
                if dir_mtime <= saved_mtime:
                    logger.info("No new experiment files — skipping file index (will still check prometheus.db)")
                    # Still index prometheus.db even if files haven't changed
                    _index_prometheus_experiments(start_embedding_server())
                    return 0
            with open(dir_mtime_path, 'w') as f:
                f.write(str(dir_mtime))
        except:
            pass
    elif os.path.exists(EXPERIMENTS_DIR):
        try:
            with open(dir_mtime_path, 'w') as f:
                f.write(str(EXPERIMENTS_DIR.stat().st_mtime))
        except:
            pass

    # Collect experiment files
    experiment_files = []
    if EXPERIMENTS_DIR.exists():
        for f in EXPERIMENTS_DIR.glob("**/*"):
            if f.is_file() and f.suffix in (".md", ".txt", ".json", ".py", ".jsonl"):
                experiment_files.append(f)

    logger.info(f"Found {len(experiment_files)} experiment files")

    # Check what's already indexed
    indexed_hashes = {}
    failed_paths = set()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT source_path, content_hash FROM documents WHERE doc_type='experiment'"
        ).fetchall()
        indexed_hashes = {r["source_path"]: r["content_hash"] for r in rows}

        # Load failed files (for reindex)
        if reindex_failed:
            failed_rows = conn.execute("SELECT path FROM failed_files").fetchall()
            failed_paths = {r["path"] for r in failed_rows}

    # Process files
    new_count = 0
    skip_count = 0
    fail_count = 0
    batch_texts = []
    batch_meta = []

    for fpath in experiment_files:
        fpath_str = str(fpath)

        # Skip binary files
        if is_binary_file(fpath):
            skip_count += 1
            continue

        # Fast skip: if file hasn't been modified since last index, skip MD5 check
        if incremental and not force and last_index_time > 0:
            try:
                mtime = fpath.stat().st_mtime
                if mtime < last_index_time and fpath_str in indexed_hashes:
                    skip_count += 1
                    continue
            except OSError:
                pass

        try:
            content = fpath.read_text(errors="ignore")
            content_hash = hashlib.md5(content.encode()).hexdigest()

            # Skip if already indexed and unchanged (unless reindexing or force)
            if not force and incremental and fpath_str in indexed_hashes:
                if not reindex_failed or fpath_str not in failed_paths:
                    if indexed_hashes[fpath_str] == content_hash:
                        continue

            # Prepare text for embedding (smart extraction + sanitization)
            text = prepare_text_for_embedding(content, fpath)

            if not text or len(text.strip()) < 20:
                skip_count += 1
                continue

            batch_texts.append(text)
            batch_meta.append({
                "path": fpath_str,
                "title": extract_title(content, fpath),
                "hash": content_hash,
                "preview": extract_preview(content, fpath),
            })
            new_count += 1

            # Batch embed every EMBED_BATCH files (big batch saturates the GPU)
            if len(batch_texts) >= EMBED_BATCH:
                success = _store_batch_with_retry(batch_texts, batch_meta, url)
                if not success:
                    fail_count += len(batch_texts)
                batch_texts = []
                batch_meta = []
                logger.info(f"  Indexed {new_count} files (skipped {skip_count}, failed {fail_count})...")

        except Exception as e:
            logger.warning(f"Error processing {fpath}: {e}")
            _record_failure(fpath_str, str(e))
            fail_count += 1

    # Final batch
    if batch_texts:
        _store_batch_with_retry(batch_texts, batch_meta, url)

    # Also index Prometheus experiments from SQLite
    _index_prometheus_experiments(url)

    # Update stats
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO index_stats (key, value, updated_at) VALUES (?, ?, ?)",
            ("last_index", datetime.now().isoformat(), time.time())
        )
        conn.execute(
            "INSERT OR REPLACE INTO index_stats (key, value, updated_at) VALUES (?, ?, ?)",
            ("experiment_count", str(new_count), time.time())
        )
        # Clear failed files that were successfully reindexed
        if reindex_failed:
            conn.execute("DELETE FROM failed_files")

    elapsed = time.time() - start
    logger.info(f"Indexing complete: {new_count} indexed, {skip_count} skipped, "
                f"{fail_count} failed in {elapsed:.1f}s")
    if force:
        logger.info("Force reindex complete — titles updated for all docs")

    # Save last index time for incremental mtime-based fast path
    try:
        with open(last_index_path, 'w') as f:
            f.write(str(time.time()))
    except:
        pass

    return new_count


def _store_batch_with_retry(texts: List[str], meta: List[Dict], url: str):
    """
    Embed and store a batch with retry logic.
    
    Strategy:
    1. Try full batch (EMBED_BATCH items)
    2. On failure, try half batch
    3. On failure, try individual items with 500 char limit
    4. Track permanently failed files
    """
    # Try full batch
    try:
        embeddings = get_embeddings_safe(texts, url, batch_size=EMBED_BATCH)
        if embeddings is not None:
            _store_embeddings(embeddings, texts, meta)
            return True
    except Exception as e:
        logger.warning(f"Full batch failed: {e}")

    # Try half batches
    for half in [16, 8]:
        for i in range(0, len(texts), half):
            chunk_texts = texts[i:i + half]
            chunk_meta = meta[i:i + half]
            try:
                embeddings = get_embeddings_safe(chunk_texts, url, batch_size=half)
                if embeddings is not None:
                    _store_embeddings(embeddings, chunk_texts, chunk_meta)
            except Exception as e:
                logger.warning(f"Half batch failed ({len(chunk_texts)} items): {e}")
                # Record individual failures
                for m in chunk_meta:
                    _record_failure(m["path"], str(e))

    # Try individual items with progressive truncation
    for i, (text, m) in enumerate(zip(texts, meta)):
        if _is_already_stored(m["path"], m["hash"]):
            continue
        
        for max_chars in [1000, 500, 250]:
            try:
                shortened = text[:max_chars]
                if len(shortened.strip()) < 20:
                    continue
                emb = get_embeddings([shortened], url)
                _store_single_embedding(emb[0], shortened, m)
                break
            except Exception as e:
                if max_chars == 250:  # Last attempt
                    logger.warning(f"  Permanently failed: {m['path']}: {e}")
                    _record_failure(m["path"], str(e))
                time.sleep(0.05)  # Rate limit between retries

    return True


def _is_already_stored(path: str, content_hash: str) -> bool:
    """Check if a file is already stored with the same hash."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT content_hash FROM documents WHERE source_path=? AND content_hash=?",
            (path, content_hash)
        ).fetchone()
        return row is not None


def _store_embeddings(embeddings: np.ndarray, texts: List[str], meta: List[Dict]):
    """Store a batch of embeddings."""
    embedding_dim = embeddings.shape[1] if embeddings.ndim > 1 else 0

    with get_db() as conn:
        for i in range(len(meta)):
            if i >= len(embeddings):
                break
            m = meta[i]
            emb_file = f"doc_{hashlib.md5(m['path'].encode()).hexdigest()[:12]}.npy"
            emb_path = EMBEDDINGS_DIR / emb_file
            np.save(str(emb_path), embeddings[i])

            conn.execute(
                "INSERT OR REPLACE INTO documents "
                "(doc_type, source_path, title, content_hash, content_preview, "
                "embedding_file, created_at, indexed_at, embedding_dim) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("experiment", m["path"], m["title"], m["hash"],
                 m["preview"], str(emb_path), time.time(), time.time(), embedding_dim)
            )
            # Clear from failed if it was there
            conn.execute("DELETE FROM failed_files WHERE path=?", (m["path"],))


def _store_single_embedding(embedding, text: str, meta: Dict):
    """Store a single embedding."""
    emb_file = f"doc_{hashlib.md5(meta['path'].encode()).hexdigest()[:12]}.npy"
    emb_path = EMBEDDINGS_DIR / emb_file
    np.save(str(emb_path), embedding)

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO documents "
            "(doc_type, source_path, title, content_hash, content_preview, "
            "embedding_file, created_at, indexed_at, embedding_dim) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("experiment", meta["path"], meta["title"], meta["hash"],
             meta["preview"], str(emb_path), time.time(), time.time(), len(embedding))
        )
        conn.execute("DELETE FROM failed_files WHERE path=?", (meta["path"],))


def _record_failure(path: str, error: str):
    """Record a failed file for later retry."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO failed_files (path, error, attempts, last_attempt) "
            "VALUES (?, ?, COALESCE((SELECT attempts FROM failed_files WHERE path=?) + 1, 1), ?)",
            (path, error[:500], path, time.time())
        )


def _index_prometheus_experiments(url: str, only_missing: bool = False):
    """Index experiments from Prometheus SQLite.

    When *only_missing* is True (default for the guard path), skip
    experiments that already have an embedding stored in rag.db.
    This turns a 30K-embed reindex into a targeted catch-up of the
    actual drift (typically < 200 items) and avoids the 300 s cron
    timeout that the old all-experiments path hit regularly.
    """
    prometheus_db = _resolve_prometheus_db()
    if not prometheus_db.exists():
        return

    conn = sqlite3.connect(str(prometheus_db), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row

    if only_missing:
        # Collect IDs already indexed in rag.db so we can exclude them.
        rag_db = HERMES_HOME / "rag" / "rag.db"
        already = set()
        if rag_db.exists():
            try:
                rc = sqlite3.connect(f"file:{rag_db}?mode=ro", uri=True, timeout=5)
                rc.execute("PRAGMA busy_timeout=3000")
                already = {
                    r[0] for r in rc.execute(
                        "SELECT id FROM experiments WHERE embedding_file IS NOT NULL"
                    )
                }
                rc.close()
            except Exception:
                pass  # if we can't read rag.db, fall through to full reindex

        experiments = [
            exp for exp in conn.execute(
                "SELECT id, hypothesis, result, domain, tags "
                "FROM experiments WHERE status='completed'"
            ).fetchall()
            if exp["id"] not in already
        ]
    else:
        experiments = conn.execute(
            "SELECT id, hypothesis, result, domain, tags FROM experiments WHERE status='completed'"
        ).fetchall()
    conn.close()

    logger.info(f"Indexing {len(experiments)} Prometheus experiments...")

    batch_texts = []
    batch_meta = []

    for exp in experiments:
        text = f"Hypothesis: {exp['hypothesis'] or ''}\nResult: {exp['result'] or ''}\nDomain: {exp['domain'] or ''}"
        batch_texts.append(text[:1000])
        batch_meta.append({
            "id": exp["id"],
            "hypothesis": exp["hypothesis"],
            "result": exp["result"],
            "domain": exp["domain"],
            "tags": exp["tags"],
        })

        if len(batch_texts) >= EMBED_BATCH:
            _store_experiment_batch(batch_texts, batch_meta, url)
            batch_texts = []
            batch_meta = []

    if batch_texts:
        _store_experiment_batch(batch_texts, batch_meta, url)


def _store_experiment_batch(texts: List[str], meta: List[Dict], url: str):
    """Embed and store Prometheus experiments.

    ROOT-CAUSE FIX (2026-06-08): previously this computed `embeddings` and then
    discarded them — the experiments mirror table stored only text. With no stored
    vector, query_rag had to re-embed live and capped at the 100 oldest experiments.
    Now we persist each vector to disk (mirroring _store_embeddings for documents)
    and record embedding_file/embedding_dim so query_rag can score ALL experiments
    from stored vectors at zero per-query embedding cost.
    """
    embeddings = get_embeddings_safe(texts, url)
    if embeddings is None:
        logger.warning("Failed to embed Prometheus experiments batch")
        return

    embedding_dim = embeddings.shape[1] if embeddings.ndim > 1 else 0

    with get_db() as conn:
        for i in range(len(meta)):
            if i >= len(embeddings):
                break
            m = meta[i]
            emb_file = f"exp_{hashlib.md5(str(m['id']).encode()).hexdigest()[:12]}.npy"
            emb_path = EMBEDDINGS_DIR / emb_file
            np.save(str(emb_path), embeddings[i])
            conn.execute(
                "INSERT OR REPLACE INTO experiments "
                "(id, title, hypothesis, result, domain, tags, status, created_at, indexed_at, "
                "embedding_file, embedding_dim) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m["id"], m.get("hypothesis", "")[:100], m.get("hypothesis"),
                 m.get("result"), m.get("domain"), m.get("tags"),
                 "indexed", time.time(), time.time(),
                 str(emb_path), embedding_dim)
            )


# ── Querying ──

def query_rag(query: str, top_k: int = 5, doc_type: str = None, worker_id: str = "cli") -> List[Dict]:
    """Search the RAG index by semantic similarity using FAISS for sub-second
    search across all 68K+ indexed experiments. Falls back to sequential search
    if FAISS index is unavailable."""
    url = f"http://127.0.0.1:{EMBEDDING_PORT}"

    # Embed query — Qwen3-Embedding uses an asymmetric instruction prefix on QUERIES
    # only; documents are embedded without a prefix during indexing.
    _q = f"Instruct: Given a research question, retrieve semantically related prior experiments and findings\nQuery: {query}"
    query_emb = get_embeddings([_q], url)[0]

    # Try FAISS first — sub-second search across the full corpus
    faiss_path = RAG_DIR / "faiss_index.faiss"
    meta_path = RAG_DIR / "faiss_index.meta.json"
    if faiss_path.exists() and meta_path.exists():
        try:
            # FAISS is installed in the venv, not system python3.
            # Add venv site-packages to path so import works from any python.
            import sys as _sys
            _venv_sp = str(HERMES_HOME / "hermes-agent" / "venv" / "lib" / "python3.14" / "site-packages")
            if _venv_sp not in _sys.path:
                _sys.path.insert(0, _venv_sp)
            import faiss
            import json as _json
            index = faiss.read_index(str(faiss_path))
            with open(meta_path) as f:
                meta = _json.load(f)
            exp_ids = meta.get("exp_ids", [])

            # Search (return 2x results for filtering)
            query_vec = query_emb.reshape(1, -1).astype("float32")
            distances, indices = index.search(query_vec, top_k * 2)

            # Look up metadata for matches
            results = []
            with get_db() as conn:
                for dist, idx in zip(distances[0], indices[0]):
                    if idx < 0 or idx >= len(exp_ids):
                        continue
                    exp_id = exp_ids[idx]
                    if not exp_id:
                        continue
                    score = max(0.0, float(dist))  # IndexFlatIP: dist IS cosine similarity for normalized vectors
                    row = conn.execute(
                        "SELECT id, hypothesis, result, domain, tags FROM experiments WHERE id=?",
                        (exp_id,)
                    ).fetchone()
                    if row:
                        results.append({
                            "type": "prometheus_experiment",
                            "id": row["id"],
                            "hypothesis": (row["hypothesis"] or "")[:200],
                            "result": (row["result"] or "")[:200],
                            "domain": row["domain"],
                            "score": score,
                        })
                    if len(results) >= top_k:
                        break

            # Log and return
            if results:
                scores = [r["score"] for r in results]
                _log_query(query, len(results), sum(scores) / len(scores),
                           scores[0], results[0].get("id", ""), worker_id=worker_id)
            return results[:top_k]
        except Exception as e:
            logger.debug(f"FAISS search failed, falling back: {e}")

    # Fallback: sequential search with embedding cap
    with get_db() as conn:
        if doc_type:
            rows = conn.execute(
                "SELECT id, doc_type, source_path, title, content_preview, embedding_file "
                "FROM documents WHERE doc_type=?", (doc_type,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, doc_type, source_path, title, content_preview, embedding_file "
                "FROM documents"
            ).fetchall()
        exp_rows = conn.execute(
            "SELECT id, hypothesis, result, domain, tags, embedding_file FROM experiments"
        ).fetchall()

    if not rows and not exp_rows:
        return [{"error": "No documents indexed. Run: python3 experiment_rag.py index"}]

    MAX_EMBEDDINGS = 5000
    if len(rows) > MAX_EMBEDDINGS:
        rows = rows[:MAX_EMBEDDINGS]
    if len(exp_rows) > MAX_EMBEDDINGS:
        exp_rows = exp_rows[:MAX_EMBEDDINGS]
    MAX_EMBEDDINGS = 5000
    if len(rows) > MAX_EMBEDDINGS:
        rows = rows[:MAX_EMBEDDINGS]
    if len(exp_rows) > MAX_EMBEDDINGS:
        exp_rows = exp_rows[:MAX_EMBEDDINGS]

    # Compute similarities for documents
    results = []

    for row in rows:
        emb_path = row["embedding_file"]
        if emb_path and os.path.exists(emb_path):
            doc_emb = np.load(emb_path)
            if doc_emb.shape[-1] != query_emb.shape[-1]:
                continue  # stale-dim vector (pre-1024 straggler) — skip, don't crash the fallback
            score = float(np.dot(query_emb, doc_emb) / (
                np.linalg.norm(query_emb) * np.linalg.norm(doc_emb) + 1e-8
            ))
            results.append({
                "type": "experiment_file",
                "title": row["title"],
                "path": row["source_path"],
                "preview": row["content_preview"][:200],
                "score": score,
            })

    # Compute similarities for Prometheus experiments using STORED vectors.
    # ROOT-CAUSE FIX (2026-06-08): previously this re-embedded live and capped at
    # exp_rows[:100] (the 100 OLDEST experiments). Now we load each experiment's
    # persisted embedding and score ALL of them at zero per-query embedding cost.
    # Experiments with no stored vector yet (NULL embedding_file, pre-migration)
    # are skipped here and will be covered once the indexer (re)runs.
    for exp in exp_rows:
        emb_path = exp["embedding_file"]
        if not emb_path or not os.path.exists(emb_path):
            continue
        exp_emb = np.load(emb_path)
        if exp_emb.shape[-1] != query_emb.shape[-1]:
            continue  # stale-dim straggler (768 pre-migration) — skip, don't crash the fallback
        score = float(np.dot(query_emb, exp_emb) / (
            np.linalg.norm(query_emb) * np.linalg.norm(exp_emb) + 1e-8
        ))
        results.append({
            "type": "prometheus_experiment",
            "id": exp["id"],
            "hypothesis": (exp["hypothesis"] or "")[:200],
            "result": (exp["result"] or "")[:200],
            "domain": exp["domain"],
            "score": score,
        })

    # Sort by score, return top_k
    results.sort(key=lambda x: x["score"], reverse=True)
    top_results = results[:top_k]
    
    # Log the query for quality tracking
    if top_results:
        scores = [r["score"] for r in top_results]
        _log_query(query, len(top_results), sum(scores) / len(scores), 
                   scores[0], top_results[0].get("title", top_results[0].get("id", "")),
                   worker_id=worker_id)
    
    return top_results


def _log_query(query: str, result_count: int, avg_score: float, 
               top_score: float, top_title: str, worker_id: str = "cli"):
    """Log a query for quality tracking."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO query_log (query_text, worker_id, result_count, avg_score, "
            "top_score, top_result_title, queried_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (query[:500], worker_id, result_count, avg_score, top_score,
             top_title[:200], time.time())
        )


def query_quality_stats(days: int = 7) -> Dict:
    """Get query quality statistics for the last N days."""
    cutoff = time.time() - (days * 86400)
    with get_db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as n FROM query_log WHERE queried_at > ?",
            (cutoff,)
        ).fetchone()["n"]
        
        avg = conn.execute(
            "SELECT AVG(avg_score) as avg, AVG(top_score) as top_avg, "
            "MIN(avg_score) as min_avg, MAX(top_score) as max_top "
            "FROM query_log WHERE queried_at > ?",
            (cutoff,)
        ).fetchone()
        
        # Low quality queries (avg score < 0.4)
        low_quality = conn.execute(
            "SELECT COUNT(*) as n FROM query_log WHERE queried_at > ? AND avg_score < 0.4",
            (cutoff,)
        ).fetchone()["n"]
        
        # High quality queries (top score > 0.7)
        high_quality = conn.execute(
            "SELECT COUNT(*) as n FROM query_log WHERE queried_at > ? AND top_score > 0.7",
            (cutoff,)
        ).fetchone()["n"]
        
        # Recent queries
        recent = conn.execute(
            "SELECT query_text, avg_score, top_score, top_result_title, queried_at "
            "FROM query_log ORDER BY queried_at DESC LIMIT 10"
        ).fetchall()
        
        # Unique workers
        workers = conn.execute(
            "SELECT COUNT(DISTINCT worker_id) as n FROM query_log WHERE queried_at > ?",
            (cutoff,)
        ).fetchone()["n"]
    
    return {
        "total_queries": total,
        "avg_score": avg["avg"] or 0,
        "avg_top_score": avg["top_avg"] or 0,
        "min_avg_score": avg["min_avg"] or 0,
        "max_top_score": avg["max_top"] or 0,
        "low_quality_pct": (low_quality / total * 100) if total > 0 else 0,
        "high_quality_pct": (high_quality / total * 100) if total > 0 else 0,
        "unique_workers": workers,
        "recent": recent,
    }


# ── Server Management ──

def keep_alive():
    """Keep the embedding server running permanently."""
    logger.info("Starting permanent embedding server...")
    url = start_embedding_server()

    # Register signal handlers
    def shutdown(sig, frame):
        logger.info("Shutting down embedding server...")
        if _server_process:
            os.killpg(os.getpgid(_server_process.pid), signal.SIGTERM)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # ROOT-CAUSE FIX (2026-06-08): when the embedding server is ALREADY running,
    # start_embedding_server() returns the URL early without setting the module
    # global _server_process, so `_server_process.pid` raised AttributeError and
    # crash-looped the systemd service (Restart=always masked it; the orphaned
    # llama-server kept serving). Guard the pid access.
    _pid = _server_process.pid if _server_process else "external (already running)"
    logger.info(f"Embedding server running at {url} (pid={_pid})")
    logger.info("Press Ctrl+C to stop")

    # Keep alive
    while True:
        time.sleep(60)
        # Health check
        try:
            import urllib.request
            req = urllib.request.urlopen(f"{url}/health", timeout=5)
            if req.status != 200:
                logger.warning("Health check failed, restarting...")
                url = start_embedding_server()
        except Exception:
            logger.warning("Server unreachable, restarting...")
            url = start_embedding_server()


def status():
    """Show RAG system status."""
    init_db()

    with get_db() as conn:
        doc_count = conn.execute("SELECT COUNT(*) as n FROM documents").fetchone()["n"]
        exp_count = conn.execute("SELECT COUNT(*) as n FROM experiments").fetchone()["n"]
        # REGRESSION GUARD (2026-06-08): surface REAL current experiments lacking a
        # stored embedding. A nonzero count means the persist-embeddings path regressed
        # (the original bug was silent embedding discard). Compared against prometheus.db
        # so stale mirror rows don't raise false alarms.
        exp_missing_emb = 0
        try:
            import sqlite3 as _sq
            _prom_db = _resolve_prometheus_db()
            _pc = _sq.connect(f"file:{_prom_db}?mode=ro", uri=True, timeout=10)
            _real = set(x[0] for x in _pc.execute("SELECT id FROM experiments WHERE status='completed'"))
            _pc.close()
            _have = set(x["id"] for x in conn.execute(
                "SELECT id FROM experiments WHERE embedding_file IS NOT NULL"))
            exp_missing_emb = len(_real - _have)
        except Exception:
            exp_missing_emb = conn.execute(
                "SELECT COUNT(*) as n FROM experiments WHERE embedding_file IS NULL"
            ).fetchone()["n"]
        failed_count = conn.execute("SELECT COUNT(*) as n FROM failed_files").fetchone()["n"]
        last_index = conn.execute(
            "SELECT value FROM index_stats WHERE key='last_index'"
        ).fetchone()

    # Check embedding server
    server_running = False
    try:
        import urllib.request
        req = urllib.request.urlopen(f"http://127.0.0.1:{EMBEDDING_PORT}/health", timeout=2)
        server_running = req.status == 200
    except Exception:
        pass

    # Disk usage
    emb_size = sum(f.stat().st_size for f in EMBEDDINGS_DIR.glob("*.npy")) // (1024 * 1024)

    # Query quality stats
    stats = query_quality_stats(days=7)

    print("=== Experiment RAG Status ===")
    print(f"Embedding server: {'✓ running (systemd)' if server_running else '✗ not running'}")
    print(f"  Port: {EMBEDDING_PORT}")
    print(f"  Model: {EMBEDDING_MODEL}")
    print(f"  VRAM: ~{EMBEDDING_VRAM_MB}MB")
    print()
    print(f"Indexed documents: {doc_count}")
    print(f"Indexed experiments: {exp_count}")
    if exp_missing_emb > 0:
        print(f"  ⚠ experiments MISSING embedding: {exp_missing_emb} "
              f"(should be 0; run the index guard or `index` to fix)")
    else:
        print(f"  ✓ all experiments have stored embeddings")
    if failed_count > 0:
        print(f"Failed files: {failed_count} (run --reindex-failed to retry)")
    print(f"Embeddings on disk: {emb_size}MB")
    print(f"Last index: {last_index['value'] if last_index else 'never'}")
    
    if stats["total_queries"] > 0:
        print()
        print("=== Query Quality (7 days) ===")
        print(f"Total queries: {stats['total_queries']}")
        print(f"Unique workers: {stats['unique_workers']}")
        print(f"Avg score: {stats['avg_score']:.3f}")
        print(f"Avg top score: {stats['avg_top_score']:.3f}")
        print(f"High quality (>0.7): {stats['high_quality_pct']:.0f}%")
        print(f"Low quality (<0.4): {stats['low_quality_pct']:.0f}%")
        if stats["recent"]:
            print()
            print("Recent queries:")
            for q in stats["recent"][:5]:
                print(f"  [{q['avg_score']:.2f}] {q['query_text'][:60]}")
    
    print()
    print("Usage:")
    print("  python3 experiment_rag.py index              # Index all experiments")
    print("  python3 experiment_rag.py index --incremental # Only new/changed")
    print("  python3 experiment_rag.py index --reindex-failed  # Retry failures")
    print("  python3 experiment_rag.py query 'quantization' --top-k 5")
    print("  python3 experiment_rag.py serve               # Keep server alive")
    print("  python3 experiment_rag.py status")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Experiment RAG System")
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="Index experiments")
    p_index.add_argument("--incremental", action="store_true")
    p_index.add_argument("--reindex-failed", action="store_true",
                         help="Retry previously failed files")
    p_index.add_argument("--force", action="store_true",
                         help="Reindex ALL files even if unchanged (updates titles)")

    p_query = sub.add_parser("query", help="Search experiments")
    p_query.add_argument("query_text", help="Search query")
    p_query.add_argument("--top-k", type=int, default=5)
    p_query.add_argument("--type", help="Filter by doc type")
    p_query.add_argument("--worker-id", default="cli",
                         help="Caller identity for quality tracking (default: cli)")

    sub.add_parser("serve", help="Keep embedding server alive")
    sub.add_parser("status", help="Show status")

    args = parser.parse_args()

    if args.command == "index":
        index_experiments(args.incremental, args.reindex_failed, args.force)
    elif args.command == "query":
        try:
            results = query_rag(args.query_text, args.top_k, args.type,
                                worker_id=args.worker_id)
        except Exception as e:
            print(f"RAG unavailable: {e}")
            print("Proceeding without RAG results.")
            sys.exit(0)
        if not results:
            print("No relevant experiments found.")
        for i, r in enumerate(results):
            print(f"\n--- Result {i+1} (score: {r.get('score', 0):.3f}) ---")
            if r.get("type") == "prometheus_experiment":
                print(f"  [Experiment {r.get('id', '')}]")
                print(f"  Hypothesis: {r.get('hypothesis', '')[:150]}")
                print(f"  Result: {r.get('result', '')[:150]}")
                print(f"  Domain: {r.get('domain', '')}")
            else:
                print(f"  {r.get('title', '')}")
                print(f"  {r.get('path', '')}")
                print(f"  {r.get('preview', '')[:150]}")
    elif args.command == "serve":
        keep_alive()
    elif args.command == "status":
        status()
    else:
        parser.print_help()
