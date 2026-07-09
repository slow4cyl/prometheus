#!/usr/bin/env python3
"""
topology_common.py — Shared topology loading for the topology visualizers.

Used by update_topology_html.py (block diagram) and update_topology_3d.py (3D).

Responsibilities
----------------
1. normalize_domain()   — canonical domain merging (single source of truth).
2. load_topology()      — build edge/degree/betweenness from the routing logs,
                          with CORRECT dedupe of the two log files and an
                          incremental on-disk cache so repeat runs are cheap.

Dedupe semantics (important)
----------------------------
There are two routing logs:

    routing_log.jsonl              (original, append-only, authoritative ORDER)
    routing_log_reclassified.jsonl (a REWRITTEN copy of the first N events with
                                    merged/corrected domain labels)

They are aligned event-for-event by timestamp. The reclassified file is the
authoritative *labelling* for every event it covers; the original log is only
authoritative for events newer than the reclassified file's coverage.

The previous implementation read BOTH files and summed every line, which
double-counted ~96% of all traffic (every reclassified event was counted once
with its old label in routing_log and once with its new label in
routing_log_reclassified). The correct stream is:

    reclassified[0 : len(reclassified)]  +  original[len(reclassified) : end]

i.e. prefer reclassified labels, then append only the original-log tail that
reclassification has not yet caught up to.

Incremental cache
-----------------
Parsing ~1M JSONL lines (~730MB) every run is the dominant cost. We persist the
aggregated edge/degree counts plus, for each source file, (size_bytes, mtime,
lines_consumed, byte_offset). On the next run:

  * If the reclassified file grew, its labels for already-counted events may have
    changed in-place, so we cannot trust the old aggregate -> full rebuild.
  * If only the original log grew (common case), we resume parsing from the saved
    byte offset of the original-log tail and fold the new events into the cached
    aggregate. Cheap.

Betweenness is always recomputed from the (small) edge set — it's fast.
"""

import collections
import json
import os
import re
from collections import defaultdict

HERMES = os.path.expanduser("~/.hermes")
ROUTING_LOG = os.path.join(HERMES, "classifier", "routing_log.jsonl")
RECLASSIFIED_LOG = os.path.join(HERMES, "classifier", "routing_log_reclassified.jsonl")
CACHE_PATH = os.path.join(HERMES, "classifier", "topology_cache.json")

# Bump when the dedupe logic / merge table changes so stale caches are discarded.
CACHE_VERSION = 2


# Canonical domain merges — single source of truth for both visualizers.
_MERGES = {
    'prompt_injection_detection': 'injection_detection',
    'prompt_injection': 'injection_detection',
    'security_injection_detection': 'injection_detection',
    'security_injection': 'injection_detection',
    'adversarial_detection': 'adversarial_ml',
    'ml_safety': 'safety', 'ml_security': 'safety',
    'tardigrade_biology': 'biology',
    'ensemble': 'ensemble_methods', 'ensemble_learning': 'ensemble_methods',
    'hallucination_detection': 'injection_detection',
    'cross_pollination': 'cross_domain',
    'synthesis': 'meta_analysis', 'meta': 'meta_analysis',
    'meta_learning': 'meta_analysis', 'meta_cognition': 'meta_analysis',
    'meta_research': 'meta_analysis',
    'injection': 'injection_detection',
    'defense': 'safety', 'attack': 'adversarial_ml',
    'general': 'calibration',
    'dispatch': 'dispatch_pipeline', 'dict': 'dict_methodology',
    'rag': 'injection_detection', 'rag_dedup': 'injection_detection',
    'rag_safety': 'injection_detection',
    'embedding': 'embedding', 'embeddings': 'embedding',
    'cross_lingual': 'nlp', 'rlhf': 'calibration',
    'distillation': 'optimization',
    'financial_fraud_detection': 'finance', 'financial_markets': 'finance',
    'ai_safety': 'safety',
    'nlp_injection': 'nlp', 'nlp_safety': 'nlp',
}

_norm_cache = {}


def normalize_domain(domain):
    """Normalize a domain string to canonical form (memoized)."""
    if not domain:
        return domain
    cached = _norm_cache.get(domain)
    if cached is not None:
        return cached
    d = domain.strip().lower()
    d = re.sub(r'[\s\-/]+', '_', d)
    d = re.sub(r'[^a-z0-9_]', '', d)
    d = re.sub(r'_+', '_', d).strip('_')
    if not d:
        _norm_cache[domain] = domain
        return domain
    result = _MERGES.get(d, d)
    _norm_cache[domain] = result
    return result


def _fold_line(line, edges, degree):
    """Parse one JSONL routing event and fold it into edges/degree. Returns bytes consumed accounting handled by caller."""
    line = line.strip()
    if not line:
        return
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return
    src = normalize_domain(e.get("source", ""))
    tgt = normalize_domain(e.get("target", ""))
    if src and tgt:
        edges[(src, tgt)] += 1
        degree[src] += 1
        degree[tgt] += 1


def _file_stat(path):
    try:
        st = os.stat(path)
        return st.st_size, int(st.st_mtime)
    except FileNotFoundError:
        return None, None


def _build_aggregate(resume_offset=0, edges=None, degree=None):
    """
    Build (edges, degree) from the deduped event stream.

    Stream = all of reclassified + original-log tail beyond reclassified coverage.
    If resume_offset > 0 and edges/degree are provided, we ONLY parse the original
    log starting at that byte offset (incremental tail append) and skip the
    reclassified file entirely (caller guarantees it is unchanged).

    Returns (edges, degree, reclass_lines, orig_tail_offset).
    """
    if edges is None:
        edges = defaultdict(int)
    if degree is None:
        degree = defaultdict(int)

    reclass_lines = 0
    incremental = resume_offset > 0

    if not incremental:
        # Full build: consume the entire reclassified file first.
        if os.path.exists(RECLASSIFIED_LOG):
            with open(RECLASSIFIED_LOG) as f:
                for line in f:
                    _fold_line(line, edges, degree)
                    reclass_lines += 1

    # Now the original log. In a full build we must SKIP the first
    # `reclass_lines` events (already counted with corrected labels) and only
    # fold the tail. In an incremental build we seek straight to resume_offset.
    orig_tail_offset = resume_offset
    if os.path.exists(ROUTING_LOG):
        with open(ROUTING_LOG) as f:
            if incremental:
                f.seek(resume_offset)
                for line in f:
                    _fold_line(line, edges, degree)
                orig_tail_offset = f.tell()
            else:
                skipped = 0
                for line in f:
                    if skipped < reclass_lines:
                        skipped += 1
                        continue
                    _fold_line(line, edges, degree)
                orig_tail_offset = f.tell()

    return edges, degree, reclass_lines, orig_tail_offset


def _compute_betweenness(edges, degree):
    """Brandes betweenness centrality. Fast on the small (~65 node) domain graph."""
    nodes = set(degree.keys())
    adj = defaultdict(list)
    for (s, t) in edges:
        adj[s].append(t)
    bc = defaultdict(float)
    for s in nodes:
        stack = []
        preds = defaultdict(list)
        sigma = defaultdict(int); sigma[s] = 1
        dist = defaultdict(lambda: -1); dist[s] = 0
        queue = collections.deque([s])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)
        delta = defaultdict(float)
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
            if w != s:
                bc[w] += delta[w]
    n = len(nodes)
    norm = 1.0 / ((n - 1) * (n - 2)) if n > 2 else 1.0
    return {k: v * norm for k, v in bc.items()}


def _load_cache():
    try:
        with open(CACHE_PATH) as f:
            c = json.load(f)
        if c.get("version") != CACHE_VERSION:
            return None
        return c
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_cache(edges, degree, reclass_lines, orig_tail_offset):
    rc_size, rc_mtime = _file_stat(RECLASSIFIED_LOG)
    orig_size, orig_mtime = _file_stat(ROUTING_LOG)
    payload = {
        "version": CACHE_VERSION,
        "reclass": {"size": rc_size, "mtime": rc_mtime, "lines": reclass_lines},
        "orig": {"size": orig_size, "mtime": orig_mtime, "tail_offset": orig_tail_offset},
        # serialize edges as "src\ttgt" -> count (tuple keys aren't JSON-able)
        "edges": {f"{s}\t{t}": c for (s, t), c in edges.items()},
        "degree": dict(degree),
    }
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, CACHE_PATH)


def load_topology(use_cache=True):
    """
    Load topology from the routing logs with correct dedupe and incremental cache.

    Returns (edges, degree, betweenness) where:
      edges:   dict[(src, tgt)] -> count
      degree:  dict[domain] -> total traffic (in + out)
      betweenness: dict[domain] -> normalized Brandes betweenness

    Set use_cache=False to force a full rebuild (ignores and overwrites cache).
    """
    cache = _load_cache() if use_cache else None

    rc_size, rc_mtime = _file_stat(RECLASSIFIED_LOG)
    orig_size, _ = _file_stat(ROUTING_LOG)

    can_incremental = (
        cache is not None
        # reclassified file unchanged -> its in-place label rewrites haven't moved
        and cache["reclass"]["size"] == rc_size
        and cache["reclass"]["mtime"] == rc_mtime
        # original log only grew (or stayed same) -> resume from saved tail offset
        and orig_size is not None
        and orig_size >= cache["orig"]["tail_offset"]
    )

    if can_incremental:
        edges = defaultdict(int)
        degree = defaultdict(int)
        for k, v in cache["edges"].items():
            s, t = k.split("\t", 1)
            edges[(s, t)] = v
        for k, v in cache["degree"].items():
            degree[k] = v
        edges, degree, _, orig_tail_offset = _build_aggregate(
            resume_offset=cache["orig"]["tail_offset"], edges=edges, degree=degree
        )
        reclass_lines = cache["reclass"]["lines"]
    else:
        edges, degree, reclass_lines, orig_tail_offset = _build_aggregate()

    if use_cache:
        try:
            _save_cache(edges, degree, reclass_lines, orig_tail_offset)
        except OSError:
            pass  # cache is best-effort; never fail the run over it

    betweenness = _compute_betweenness(edges, degree)
    return dict(edges), dict(degree), betweenness


def db_hub_concentration(threshold=0.05):
    """Compute hub concentration from the experiments table (actual outcomes).

    A domain is a "hub" only if it has >threshold share of total experiments.
    This avoids artificially forcing 6 nodes into a hub category when the
    distribution is actually spread out.

    Returns (hub_domains, concentration_pct, total_experiments) or
    (None, 0.0, 0) if the DB is unreachable.
    """
    import sqlite3
    db = os.path.join(HERMES, "prometheus.db")
    if not os.path.exists(db):
        return None, 0.0, 0
    try:
        conn = sqlite3.connect(db, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        rows = conn.execute(
            "SELECT domain, COUNT(*) as cnt FROM experiments "
            "WHERE domain IS NOT NULL GROUP BY domain ORDER BY cnt DESC"
        ).fetchall()
        conn.close()
        if not rows:
            return None, 0.0, 0
        total = sum(r[1] for r in rows)
        hubs = [r[0] for r in rows if r[1] / total > threshold]
        hub_traffic = sum(r[1] for r in rows if r[1] / total > threshold)
        concentration = 100.0 * hub_traffic / total if total else 0.0
        return hubs, concentration, total
    except Exception:
        return None, 0.0, 0


def load_topology_from_db(return_metadata=False):
    """Load topology directly from the experiments table (ground truth).

    Returns (edges, degree, betweenness) in the same format as load_topology(),
    but computed from actual experiment counts and cross-domain relationships
    instead of the inflated routing log.

    Edges are derived from:
    1. Worker results queue_additions (workers suggesting cross-domain follow-ups)
    2. Experiment tags containing TRANSFER (cross-domain transfer experiments)
    3. Centroid similarity fallback for edge-less domains

    When return_metadata=True, returns (edges, degree, betweenness, edge_metadata).
    edge_metadata[(s,t)] = {
        'provenance': [...],       # ['queue_addition', 'transfer_tag', 'centroid_fallback']
        'transfer_count': int,      # number of events that produced this edge
        'is_fallback': bool,       # True if this edge only exists via centroid similarity
    }
    """
    import sqlite3
    db = os.path.join(HERMES, "prometheus.db")
    if not os.path.exists(db):
        return ({}, {}, {}) if not return_metadata else ({}, {}, {}, {})

    conn = sqlite3.connect(db, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass

    # 1. Domain experiment counts -> degree
    degree = {}
    for row in conn.execute(
        "SELECT domain, COUNT(*) FROM experiments "
        "WHERE domain IS NOT NULL GROUP BY domain"
    ).fetchall():
        degree[row[0]] = row[1]

    # Load excluded domains (redirected or malformed) so they don't leak into
    # edges as phantom source domains from stale worker_results rows.
    _excluded = {'uncategorized', 'unclassified_pending', 'split_per_experiment', ''}
    try:
        for row in conn.execute("SELECT old_domain FROM domain_redirects").fetchall():
            _excluded.add(row[0])
        for row in conn.execute("SELECT malformed_domain FROM domain_malformed_log").fetchall():
            _excluded.add(row[0])
    except Exception:
        pass

    # Track edge provenance and transfer counts
    edge_weights = defaultdict(int)
    edge_provenance = defaultdict(set)  # (s,t) -> set of provenance strings

    # One combined word-boundary regex over every domain, matched ONCE per row,
    # instead of recompiling `\bdomain\b` for all ~266 domains on every row
    # (that was ~19M regex compiles and dominated the whole build). Alternation
    # + `\b` is semantically identical to the per-domain existence test: the
    # match text is the domain itself (text is lowercased, so only lowercase
    # domains can match a case-sensitive pattern), and `\b` guarantees each
    # alternative only fires on a full-word hit, so alternation order is
    # irrelevant. set() collapses repeats to the same once-per-row semantics.
    domain_alt = (
        re.compile(r'\b(?:' + '|'.join(re.escape(d) for d in degree) + r')\b')
        if degree else None
    )

    # 2. Cross-domain edges from worker_results queue_additions
    #    Workers suggest follow-up experiments mentioning other domains
    #    NOTE: Uses word-boundary matching (\b) to avoid false positives
    #    from substring matches (e.g. "rl" matching inside "overlap")
    if domain_alt is not None:
        for row in conn.execute(
            "SELECT domain, queue_additions FROM worker_results "
            "WHERE queue_additions IS NOT NULL AND queue_additions != ''"
        ).fetchall():
            source = row[0]
            if source in _excluded:
                continue  # skip redirected/malformed domains — no phantom edges
            queue_lower = row[1].lower().replace('_', ' ')
            for domain in set(domain_alt.findall(queue_lower)):
                if domain != source:
                    edge_weights[(source, domain)] += 1
                    edge_provenance[(source, domain)].add('queue_addition')

    # 3. Cross-domain edges from TRANSFER-tagged experiments
    #    NOTE: Uses word-boundary matching (\\b) to avoid false positives
    #    from substring matches (same fix as queue_additions path above)
    if domain_alt is not None:
        for row in conn.execute(
            "SELECT domain, tags FROM experiments "
            "WHERE tags LIKE '%TRANSFER%' AND domain IS NOT NULL"
        ).fetchall():
            source = row[0]
            tags_lower = (row[1] or '').lower().replace('_', ' ')
            for domain in set(domain_alt.findall(tags_lower)):
                if domain != source:
                    edge_weights[(domain, source)] += 1
                    edge_provenance[(domain, source)].add('transfer_tag')

    # 4. Fallback: domains with experiments but zero edges get one minimal edge
    #    to the most semantically similar domain (by centroid cosine similarity).
    domains_with_edges = set()
    for s, t in edge_weights:
        domains_with_edges.add(s)
        domains_with_edges.add(t)

    edge_less = [d for d in degree if d not in domains_with_edges]
    if edge_less and os.path.exists(os.path.join(HERMES, "domain_embeddings.json")):
        try:
            import json as _json, math as _math
            with open(os.path.join(HERMES, "domain_embeddings.json")) as _f:
                centroids = _json.load(_f)

            def _cosine(a, b):
                dot = sum(ai * bi for ai, bi in zip(a, b))
                na = _math.sqrt(sum(x * x for x in a))
                nb = _math.sqrt(sum(x * x for x in b))
                return dot / (na * nb) if na and nb else 0.0

            for d in edge_less:
                if d not in centroids:
                    continue
                d_emb = centroids[d]
                best = None
                best_sim = -1.0
                for other, o_emb in centroids.items():
                    if other == d or other not in degree:
                        continue
                    sim = _cosine(d_emb, o_emb)
                    if sim > best_sim:
                        best_sim = sim
                        best = other
                if best and best_sim > 0.5:
                    edge_weights[(d, best)] = 1
                    edge_provenance[(d, best)].add('centroid_fallback')
        except Exception:
            pass  # embedding cache unavailable — skip fallback

    conn.close()

    # Convert to the format expected by visualizers
    edges = {(s, t): w for (s, t), w in edge_weights.items()}

    # Compute betweenness using real Brandes algorithm (not weighted degree).
    # The previous implementation summed edge weights per node — that's degree
    # centrality, not betweenness. Real betweenness counts how many shortest
    # paths pass through each node, which is what makes "bridge" analysis valid.
    betweenness = _compute_betweenness(edges, degree)

    if not return_metadata:
        return dict(edges), degree, dict(betweenness)

    # Build edge metadata with provenance info
    # Quality metrics (confirm_rate, novelty_rate) are lightweight extras
    edge_metadata = {}
    for (s, t) in edges:
        prov = sorted(edge_provenance.get((s, t), ['unknown']))
        is_fb = 'centroid_fallback' in prov
        edge_metadata[(s, t)] = {
            'provenance': prov,
            'transfer_count': edges[(s, t)],
            'is_fallback': is_fb,
        }

    return dict(edges), degree, dict(betweenness), edge_metadata
