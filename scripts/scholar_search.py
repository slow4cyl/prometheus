#!/usr/bin/env python3
"""Scholarly-index retrieval leg for the novelty lanes (Semantic Scholar + OpenAlex).

Why this exists: the FINDER (novelty_audit.py) and the --adjudicate JUDGE
(novelty_calibration.py) check "is this in the literature?" with an LLM + the
OpenRouter web plugin. That retrieval is porous for CROSS-DISCIPLINARY
counterparts — the confirmed double-miss: #61561 ("communication benefit ∝ task
interdependence") was corroborated NOT_FOUND while Thompson 1967 / Van de Ven
et al. 1976 / IC3Net (ICLR 2019) / Marlow et al. 2018 sat in the indexes.
Novelty is a claim about the literature, and an LLM's web-search habits are not
the literature. This module puts ACTUAL indexed papers (title/abstract/year/
venue/ids) in front of the model so it READS instead of recalls.

Two free, keyless APIs with deliberately different corpora:
  Semantic Scholar Graph API  ~200M papers — strong CS/biomed, indexes arXiv
  OpenAlex                    ~250M works  — strong social science / org theory
Cross-disciplinary coverage is the point: the blind spot is exactly the
counterpart living in ANOTHER field's vocabulary.

Fail-soft everywhere: timeouts / 429s / schema surprises → fewer or zero hits,
and callers proceed web-only exactly as before this module existed. A global
per-API politeness interval keeps concurrent audits (novelty_audit runs a
ThreadPoolExecutor) from hammering the shared unauthenticated pools; the S2
pool 429s readily, so its calls are globally serialized at ~1.1s spacing and
search_indexes carries a hard wall-clock budget so the leg can never stall an
audit past a few seconds.

CLI probe (no DB, no LLM):
  python3 scholar_search.py "task interdependence communication team performance"
"""

import os
import re
import sys
import threading
import time

import requests

S2_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OA_URL = "https://api.openalex.org/works"
OA_MAILTO = os.environ.get("OPENALEX_MAILTO", "")  # OpenAlex "polite pool" — set your contact email for faster, more reliable API access


def _s2_key():
    """Semantic Scholar API key (optional). The UNAUTHENTICATED pool is shared across
    every anonymous caller on the internet, so it 429s constantly — a FREE key
    (apisvc form at https://www.semanticscholar.org/product/api → "Request an API
    Key") gives a dedicated ~1 req/sec and the 429s largely stop. Read from the env
    or ~/.hermes/.env (S2_API_KEY or SEMANTIC_SCHOLAR_API_KEY); absent → keyless, as
    before. This is the one lever that makes the index leg reliable."""
    k = os.environ.get("S2_API_KEY") or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if k:
        return k.strip()
    try:
        with open(os.path.expanduser("~/.hermes/.env")) as f:
            for line in f:
                s = line.strip()
                if s.startswith(("S2_API_KEY", "SEMANTIC_SCHOLAR_API_KEY")) and "=" in s:
                    return s.split("=", 1)[1].strip().strip('"').strip("'") or None
    except Exception:
        pass
    return None


S2_KEY = _s2_key()

# global politeness: minimum interval per API across ALL threads in the process.
# With a key S2 grants a dedicated 1 req/sec, so we can pace tighter; keyless we
# stay conservative on the shared pool that 429s readily.
_S2_INTERVAL = 1.0 if S2_KEY else 1.1
_OA_INTERVAL = 0.15
_locks = {"s2": threading.Lock(), "oa": threading.Lock()}
_last = {"s2": 0.0, "oa": 0.0}


def _pace(api, interval):
    """Block until this API's politeness interval has elapsed (thread-safe)."""
    with _locks[api]:
        wait = _last[api] + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[api] = time.monotonic()


def s2_search(query, limit=8, timeout=8):
    """Semantic Scholar relevance search → [{title, abstract, year, venue, ids,
    cited, src}]. One polite retry on 429 (shared unauthenticated pool)."""
    params = {"query": query, "limit": limit,
              "fields": "title,abstract,year,venue,externalIds,citationCount"}
    headers = {"x-api-key": S2_KEY} if S2_KEY else {}
    _pace("s2", _S2_INTERVAL)
    r = requests.get(S2_URL, params=params, timeout=timeout, headers=headers)
    if r.status_code == 429:
        time.sleep(2.5)
        _pace("s2", _S2_INTERVAL)
        r = requests.get(S2_URL, params=params, timeout=timeout, headers=headers)
    r.raise_for_status()
    out = []
    for p in (r.json().get("data") or []):
        ids = p.get("externalIds") or {}
        out.append({
            "title": (p.get("title") or "").strip(),
            "abstract": (p.get("abstract") or "")[:1200],
            "year": p.get("year"),
            "venue": (p.get("venue") or "")[:120],
            "ids": {k: str(v) for k, v in ids.items() if k in ("DOI", "ArXiv", "PubMed")},
            "cited": p.get("citationCount"),
            "src": "S2",
        })
    return out


def _oa_abstract(inv):
    """OpenAlex ships abstracts as {word: [positions]} — reconstruct the text."""
    if not inv:
        return ""
    pairs = [(pos, w) for w, ps in inv.items() for pos in ps]
    return " ".join(w for _, w in sorted(pairs))[:1200]


def openalex_search(query, limit=8, timeout=8):
    """OpenAlex full-text relevance search → same hit shape as s2_search."""
    _pace("oa", _OA_INTERVAL)
    r = requests.get(OA_URL, params={
        "search": query, "per-page": limit, "mailto": OA_MAILTO,
        "select": "title,publication_year,doi,abstract_inverted_index,"
                  "cited_by_count,primary_location"}, timeout=timeout)
    r.raise_for_status()
    out = []
    for w in (r.json().get("results") or []):
        src = ((w.get("primary_location") or {}).get("source") or {})
        doi = (w.get("doi") or "").replace("https://doi.org/", "")
        out.append({
            "title": (w.get("title") or "").strip(),
            "abstract": _oa_abstract(w.get("abstract_inverted_index")),
            "year": w.get("publication_year"),
            "venue": (src.get("display_name") or "")[:120],
            "ids": {"DOI": doi} if doi else {},
            "cited": w.get("cited_by_count"),
            "src": "OA",
        })
    return out


def _key(h):
    """Dedupe key: DOI when present, else squashed title."""
    doi = (h.get("ids") or {}).get("DOI", "")
    if doi:
        return "doi:" + doi.lower()
    return "t:" + re.sub(r"[^a-z0-9]+", "", (h.get("title") or "").lower())[:80]


def search_indexes(queries, per_query=6, max_total=18, time_budget=8.0):
    """Run each query against both indexes, dedupe (DOI else title), keep API
    relevance order. Hard wall-clock budget: when exceeded, remaining calls are
    skipped — partial context is still context. Never raises."""
    t0 = time.monotonic()
    hits, seen = [], set()
    for q in [q for q in queries if q and str(q).strip()][:4]:
        for fn in (s2_search, openalex_search):
            if time.monotonic() - t0 > time_budget or len(hits) >= max_total:
                return hits[:max_total]
            try:
                for h in fn(str(q), limit=per_query):
                    k = _key(h)
                    if h["title"] and k not in seen:
                        seen.add(k)
                        hits.append(h)
            except Exception:  # noqa: BLE001 — the index leg must never sink an audit
                continue
    return hits[:max_total]


def format_hits(hits, max_chars=6500):
    """Numbered prompt block: [n] Title (Year, Venue) [ids] + truncated abstract."""
    lines, used = [], 0
    for i, h in enumerate(hits, 1):
        ids = " ".join(f"{k}:{v}" for k, v in (h.get("ids") or {}).items())
        head = f"[{i}] {h['title']} ({h.get('year') or '?'}, {h.get('venue') or '?'})"
        if ids:
            head += f" [{ids}]"
        ab = (h.get("abstract") or "").strip()
        entry = head + (f"\n    {ab[:400]}" if ab else "")
        if used + len(entry) > max_chars:
            break
        used += len(entry)
        lines.append(entry)
    return "\n".join(lines)


if __name__ == "__main__":
    qs = sys.argv[1:] or ["task interdependence communication team performance"]
    got = search_indexes(qs)
    print(f"{len(got)} hits for {qs!r}:\n")
    print(format_hits(got))
