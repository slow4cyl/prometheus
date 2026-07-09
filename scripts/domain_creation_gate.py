#!/usr/bin/env python3
"""Domain creation gate — prevents taxonomy fragmentation.

When a worker result arrives with a domain that doesn't exist in the
canonical set (>=10 experiments), this module checks if it's close enough
to an existing domain to redirect. Prevents the system from minting new
domain names faster than knowledge accumulates.

Usage:
    from domain_creation_gate import resolve_domain, is_canonical, get_canonical_domains

    resolved = resolve_domain("veterinary_medicine")
    # -> "medicine" (if similarity > threshold)

    if not is_canonical("textile_science"):
        # domain is provisional — gate found no close match
        ...

Gating logic (in priority order):
    1. Exact match in normalize_domain() merges → already handled upstream
    2. Name-overlap check: small domain name is substring of canonical (or vice versa)
    3. Token-set similarity: Jaccard overlap of underscore-split tokens
    4. Embedding similarity (if server available): cosine distance < 0.35
    5. If nothing matches → allow as provisional (but log it)
"""

import os
import re
import sqlite3
import logging
import fcntl
from typing import Optional, Tuple, Dict, Set

logger = logging.getLogger(__name__)

# Thresholds
NAME_OVERLAP_THRESHOLD = 0.65   # substring match (lower to catch prefix/suffix variants)
TOKEN_JACCARD_THRESHOLD = 0.40  # token-set Jaccard similarity
TOKEN_CONTAINMENT_THRESHOLD = 0.80  # fraction of small domain's tokens found in canonical
EMBEDDING_THRESHOLD = 0.35      # cosine distance (lower = more similar)

_DB_PATH = os.environ.get("PROMETHEUS_DB",
                          os.path.join(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
                                       "prometheus.db"))

CanonicalCache = None  # lazily loaded


def _get_db():
    """Get database connection with retry (db_retry pattern)."""
    from db_retry import get_db
    return get_db(_DB_PATH)


def get_canonical_domains(min_count: int = 10) -> Dict[str, int]:
    """Return {domain: experiment_count} for domains with >= min_count experiments."""
    global CanonicalCache
    if CanonicalCache is not None:
        return CanonicalCache
    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("""SELECT domain, COUNT(*) as cnt FROM experiments
                     WHERE typeof(created_at) = 'real'
                     GROUP BY domain HAVING cnt >= ?""", (min_count,))
        CanonicalCache = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to load canonical domains: {e}")
        CanonicalCache = {}
    return CanonicalCache


def is_canonical(domain: str) -> bool:
    """Check if a domain has enough experiments to be canonical."""
    canonical = get_canonical_domains()
    return domain in canonical


def _name_overlap(small: str, big: str) -> float:
    """Check if one name is a substring of the other, return overlap ratio."""
    if small == big:
        return 1.0
    if small in big:
        return len(small) / len(big)
    if big in small:
        return len(big) / len(small)
    return 0.0


def _token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity of underscore-split token sets."""
    tokens_a = set(a.split('_'))
    tokens_b = set(b.split('_'))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _token_containment(small_tokens: set, big_tokens: set) -> float:
    """Fraction of small domain's tokens found in big domain's tokens.
    High containment means small is a specialization of big."""
    if not small_tokens:
        return 0.0
    return len(small_tokens & big_tokens) / len(small_tokens)


def _embedding_similarity(domain: str, candidate: str) -> Optional[float]:
    """Compute embedding-based similarity between two domain names.
    Returns cosine distance (lower = more similar) or None if unavailable."""
    try:
        sys_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'embedding_domain_classifier.py')
        if not os.path.exists(sys_path):
            return None
        import importlib.util
        spec = importlib.util.spec_from_file_location("emb_cls", sys_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if hasattr(mod, 'classify_embedding'):
            # Use the embedding server to get vectors for both domain names
            import urllib.request
            import json
            port = 9150
            # Try to get similarity via the embed server
            for text in [domain.replace('_', ' '), candidate.replace('_', ' ')]:
                req = urllib.request.Request(
                    f"http://localhost:{port}/embed",
                    data=json.dumps({"text": text}).encode(),
                    headers={"Content-Type": "application/json"}
                )
                urllib.request.urlopen(req, timeout=2)
            # If server is up, use classify_embedding to check
            res_a = mod.classify_embedding(domain.replace('_', ' '))
            res_b = mod.classify_embedding(candidate.replace('_', ' '))
            # If both classify to the same domain, they're similar
            if isinstance(res_a, tuple):
                res_a = res_a[0]
            if isinstance(res_b, tuple):
                res_b = res_b[0]
            if res_a == res_b:
                return 0.1  # very similar
            return None
    except Exception:
        pass
    return None


def decompose_concatenation(domain: str, known: Optional[Set[str]] = None) -> Optional[list]:
    """Detect separator-less concatenation of 2+ known domain labels.

    The classifier's multi-label output path occasionally drops the separator,
    emitting e.g. "cybersecuritypharmacology" (cybersecurity+pharmacology) or
    "biologyimmunologyreliability" (biology+immunology+reliability). These are a
    SINGLE token (no '_' or ' '), so every token-based check in resolve_domain()
    silently misses them and the malformed label is admitted as "provisional".

    This greedily decomposes a separator-less domain into known labels
    (longest-match first). Returns the list of component domains if it splits
    cleanly into >=2 of them, else None.

    IMPORTANT: a clean decomposition is NOT licence to flatten the label to its
    first component — that would erase the cross-domain edge a transfer
    experiment encodes. The caller records the malformed label for per-experiment
    re-classification instead. (See architecture-map: the system's unit of value
    is the FLOW between domains, not the domain in isolation.)
    """
    if not domain:
        return None
    # NOTE: we no longer skip domains containing '_' or ' '. The delimiter-loss
    # bug can produce PARTIAL glue-ups like 'nlpmedical_text_classification'
    # (nlp + medical_text_classification) where the separator survives in the
    # second component but is missing between the first and second. We strip all
    # separators and try decomposition on the flat form.
    if known is None:
        known = get_known_label_vocabulary()
    # Build a match table keyed by the SEPARATOR-STRIPPED form of every known
    # label, mapping back to the canonical label. The glue-up drops separators,
    # so a multi-word component like 'machine_learning' appears in the blob as
    # 'machinelearning' and 'transfer_learning' as 'transferlearning'. Stripping
    # '_' from the vocabulary lets us recover those. Require len>=4 to avoid
    # matching trivially-short stubs, EXCEPT for canonical domains (>=10 exps)
    # where len>=3 is allowed — this covers real short domains like 'nlp'.
    # Sort longest-first for greedy longest match (prefer 'cybersecurity' over
    # an accidental shorter prefix).
    canonical_set = get_canonical_domains()
    stripped = {}
    for k in known:
        s = k.replace('_', '')
        min_len = 3 if k in canonical_set else 4
        if len(s) >= min_len and s not in stripped:
            stripped[s] = k
    keys = sorted(stripped.keys(), key=len, reverse=True)
    flat = domain.replace('_', '')

    # ── Partial glue-up detection for underscored domains ────────────────
    # A partial glue-up has underscores in the SECOND component but not between
    # the first and second (e.g. 'nlpmedical_text_classification' = 'nlp' glued
    # to 'medical_text_classification'). This MUST run before the self-match
    # check below, because the contaminated domain is in the vocab (underscored
    # domains are unconditionally kept) and its stripped form matches itself.
    if '_' in domain:
        first_part, rest = domain.split('_', 1)
        # Check if first_part is a known canonical domain with a separator-less
        # glue-up before the underscore. E.g. 'nlpmedical' -> 'nlp' + 'medical'
        for s in keys:
            if first_part.startswith(s) and len(first_part) > len(s):
                # The prefix is a known label, and there's extra text before
                # the first underscore — this is a partial glue-up.
                glued_prefix = stripped[s]
                remainder = first_part[len(s):] + '_' + rest
                return [glued_prefix, f"<unmatched:{remainder}>"]

    if flat in stripped and stripped[flat] == domain:
        return None  # exact known label — not a concatenation

    parts = []
    rem = flat
    while rem:
        for s in keys:
            if rem.startswith(s):
                parts.append(stripped[s])
                rem = rem[len(s):]
                break
        else:
            return None  # leftover that isn't a known label -> not a clean glue-up
    # Reject the degenerate self-match (a single label whose stripped form equals
    # the whole blob) and require >=2 DISTINCT components so 'xx'->['x','x'] noise
    # can't trip it.
    return parts if len(parts) >= 2 and len(set(parts)) >= 2 else None


def _load_clean_classifier_vocab() -> Set[str]:
    """Authoritative atomic-label set: the classifier's CLEAN multi-class output.

    The reclassify log's `new_domain` field is the classifier emitting a SINGLE
    canonical label per decision — these are separator-correct by construction
    and are the best ground-truth list of atomic domains. We harvest the set once.
    """
    import json as _json
    path = os.path.join(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
                        "classifier", "reclassify_log.jsonl")
    out = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                nd = r.get("new_domain")
                if nd:
                    out.add(nd)
    except Exception as e:
        logger.warning(f"Could not read classifier vocab: {e}")
    return out


_KNOWN_VOCAB_CACHE = None


def get_known_label_vocabulary() -> Set[str]:
    """Atomic domain-label vocabulary for concatenation detection.

    Sources, in trust order:
      1. classifier clean output (`new_domain` in reclassify_log) — authoritative
         atomic labels, separator-correct by construction.
      2. canonical domains (>=10 experiments).
      3. all distinct DB domains — needed for sub-threshold components like
         'reliability' (1 experiment) that real glue-ups contain.

    The DB set is CONTAMINATED with the malformed glue-ups themselves (the bug
    wrote them as domains). If left in, a blob greedily matches ITSELF and never
    decomposes. We purge contaminated labels ITERATIVELY: anchor on a trusted
    atomic core (sources 1+2 minus anything that itself decomposes), then drop
    any DB label whose stripped form splits into >=2 distinct atomic cores.
    """
    global _KNOWN_VOCAB_CACHE
    if _KNOWN_VOCAB_CACHE is not None:
        return _KNOWN_VOCAB_CACHE

    canonical = set(get_canonical_domains().keys())
    clean = _load_clean_classifier_vocab()

    # Curated atomic FRAGMENT-ROOTS: tokens the delimiter-loss bug emits as a
    # trailing/middle component of a glue-up but which never appear as a clean
    # standalone domain (so they're in neither `canonical` nor `clean`). Without
    # these, a blob like 'biologyimmunologyreliability' can't fully decompose
    # (the trailing 'reliability' has no atom to match) and survives as a self-
    # matching junk label. Each maps a fragment to its real canonical expansion,
    # observed in the reclassify log's alt_domain decompositions. Extend as new
    # fragments surface in domain_malformed_log.
    FRAGMENT_ROOTS = {
        "reliability",        # ↔ reliability_theory
        "blockchain",         # ↔ blockchain_consensus
        "security",           # standalone + fragment
        "transfer",           # ↔ transfer_learning fragment
        "cross",              # ↔ cross_domain* fragment
        "chronobiology",      # standalone domain (paleontology+chronobiology glue-up)
    }
    clean = clean | FRAGMENT_ROOTS
    db_all = set()
    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("SELECT DISTINCT domain FROM experiments WHERE domain IS NOT NULL")
        db_all = {d for (d,) in c.fetchall() if d}
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to load DB domains: {e}")

    def _strip(x):
        return x.replace('_', '')

    # Trusted atomic CORE = clean classifier labels ∪ canonical. Then remove any
    # core label that itself decomposes into 2+ OTHER core labels (defends
    # against a glue-up that sneaked into new_domain). Iterate to fixpoint.
    core = (clean | canonical)
    changed = True
    while changed:
        changed = False
        core_stripped = sorted({_strip(a) for a in core}, key=len, reverse=True)
        for label in sorted(core, key=lambda x: len(_strip(x)), reverse=True):
            flat = _strip(label)
            rem, parts = flat, []
            while rem:
                for s in core_stripped:
                    if s != flat and rem.startswith(s):
                        parts.append(s); rem = rem[len(s):]; break
                else:
                    break
            if not rem and len(parts) >= 2 and len(set(parts)) >= 2:
                core.discard(label); changed = True; break

    # Final vocabulary: trusted core ∪ DB labels that are NOT decomposable into
    # core atoms (those are the glue-ups we must exclude as match candidates).
    core_stripped = sorted({_strip(a) for a in core}, key=len, reverse=True)
    def _decomposes_into_core(label):
        flat = _strip(label)
        if flat in {_strip(a) for a in core}:
            return False  # is itself a core atom
        rem, parts = flat, []
        while rem:
            for s in core_stripped:
                if rem.startswith(s):
                    parts.append(s); rem = rem[len(s):]; break
            else:
                return False
        return len(parts) >= 2 and len(set(parts)) >= 2

    cleaned = set(core)
    for v in db_all:
        if '_' in v or ' ' in v:
            cleaned.add(v); continue           # real multi-word domain, always keep
        if _decomposes_into_core(v):
            continue                            # contaminated glue-up — exclude
        cleaned.add(v)

    _KNOWN_VOCAB_CACHE = cleaned
    return _KNOWN_VOCAB_CACHE


def record_malformed(domain: str, parts: list):
    """Audit-log a detected separator-less concatenation for later per-experiment
    re-classification. Does NOT mutate experiments — detection only.

    Uses the RetryConnection's own execute/commit (NOT conn.cursor(), which
    returns a raw sqlite3 cursor that bypasses the retry wrapper) so a transient
    'database is locked' under concurrent worker/cron load is retried rather than
    silently dropped. (Lesson learned 2026-06-15: cursor()-based writes lose the
    retry path and lose records under load.)"""
    try:
        conn = _get_db()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS domain_malformed_log (
                malformed_domain TEXT NOT NULL,
                components TEXT NOT NULL,
                first_seen REAL DEFAULT (strftime('%s','now')),
                hit_count INTEGER DEFAULT 1,
                UNIQUE(malformed_domain)
            )""")
            conn.execute("""INSERT INTO domain_malformed_log (malformed_domain, components)
                         VALUES (?, ?)
                         ON CONFLICT(malformed_domain)
                         DO UPDATE SET hit_count = hit_count + 1""",
                      (domain, "+".join(parts)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Failed to record malformed domain {domain}: {e}")


def resolve_domain(domain: str, text: str = None) -> Tuple[str, str, float]:
    """Resolve a potentially-fragmented domain to its canonical parent.

    Args:
        domain: The domain to resolve (e.g., "veterinary_medicine")
        text: Optional experiment text for embedding-based fallback

    Returns:
        (resolved_domain, reason, confidence)
        - resolved_domain: the canonical domain to use
        - reason: explanation of why this resolution was chosen
        - confidence: 0.0-1.0, how confident we are in the redirect
    """
    if not domain:
        return (domain, "empty", 0.0)

    # ── Separator-less concatenation guard (delimiter-loss bug) ──────────────
    # MUST run before the canonical early-return: the delimiter-loss bug ran long
    # enough that some malformed labels (e.g. 'biologyimmunologyreliability', 83
    # experiments) crossed the >=10 threshold and became "canonical" themselves.
    # Checking decomposition first re-flags those already-promoted junk labels.
    # The check is cheap and returns None for every legitimate single-word label
    # (verified: 0 false positives across the real domain vocabulary), so it is
    # safe ahead of the fast path. We do NOT flatten to a component — returning
    # resolved==domain leaves the caller's `if resolved != domain` guard false,
    # so no row is silently rewritten and no cross-domain edge is eroded; we only
    # record the malformed label for per-experiment re-classification.
    parts = decompose_concatenation(domain)
    if parts:
        record_malformed(domain, parts)
        logger.warning(f"Domain gate: MALFORMED concatenation {domain} = {'+'.join(parts)} "
                       f"(recorded, not flattened — needs per-experiment reclassification)")

        # Try embedding-based reclassification using the experiment text so we
        # can redirect to a real domain rather than a sentinel.  Falls back to
        # 'unclassified_pending' if the classifier is unavailable or returns
        # nothing useful.  Either way, create a redirect so domain_merge_sync
        # can migrate any remaining rows (experiments + worker_results).
        reclass_target = None
        if text:
            try:
                import importlib.util as _ilu
                _cls_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         'embedding_domain_classifier.py')
                if os.path.exists(_cls_path):
                    _spec = _ilu.spec_from_file_location("emb_cls2", _cls_path)
                    _mod = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_mod)
                    if hasattr(_mod, 'classify_embedding'):
                        _res = _mod.classify_embedding(text[:2000])
                        if isinstance(_res, tuple):
                            _res = _res[0]
                        if _res and _res != domain and '+' not in _res:
                            reclass_target = _res
            except Exception:
                pass
        if not reclass_target:
            reclass_target = 'unclassified_pending'

        record_redirect(domain, reclass_target,
                        f'malformed_concatenation:{"+".join(parts)}')
        logger.info(f"Domain gate: redirecting malformed {domain} -> {reclass_target}")
        return (reclass_target, "malformed_concatenation:" + "+".join(parts), 0.0)

    # Already canonical? No gate needed.
    canonical = get_canonical_domains()
    if domain in canonical:
        return (domain, "canonical", 1.0)

    # Check if this provisional domain should be promoted
    promotion = check_promotion(domain)
    if promotion == 'promoted':
        return (domain, "promoted", 1.0)

    best_candidate = None
    best_score = 0.0
    best_reason = ""

    small_tokens = set(domain.split('_'))

    for cand, count in canonical.items():
        cand_tokens = set(cand.split('_'))

        # 1. Name overlap (substring)
        overlap = _name_overlap(domain, cand)
        if overlap >= NAME_OVERLAP_THRESHOLD:
            if overlap > best_score:
                best_score = overlap
                best_candidate = cand
                best_reason = f"name_overlap({overlap:.2f})"

        # 2. Token containment: small domain's tokens are subset of canonical
        #    This catches "veterinary_medicine" -> "medicine", "llm_calibration" -> "calibration"
        containment = _token_containment(small_tokens, cand_tokens)
        if containment >= TOKEN_CONTAINMENT_THRESHOLD:
            # Prefer the largest canonical domain for ties
            score = containment * 0.95 + (count / 10000) * 0.05
            if score > best_score:
                best_score = score
                best_candidate = cand
                best_reason = f"token_containment({containment:.2f})"

        # 3. Shared prefix: first token matches AND canonical is much larger
        #    Catches llm_architecture -> llm_general, ml_quantization -> ml_theory
        #    Excludes generic tokens that match too many domains.
        small_tokens_list = domain.split('_')
        cand_tokens_list = cand.split('_')
        GENERIC_PREFIXES = {'science', 'engineering', 'analysis', 'methods',
                           'detection', 'learning', 'model', 'system'}
        if small_tokens_list and cand_tokens_list:
            small_prefix = small_tokens_list[0]  # actual first token
            cand_prefix = cand_tokens_list[0]
            if (small_prefix == cand_prefix
                    and small_prefix not in GENERIC_PREFIXES
                    and count >= 50  # canonical must be well-established
                    and len(cand_tokens_list) <= len(small_tokens_list)):
                score = 0.88 + (count / 10000) * 0.05
                if score > best_score:
                    best_score = score
                    best_candidate = cand
                    best_reason = f"shared_prefix('{small_prefix}')"

        # 4. Token Jaccard
        jaccard = _token_jaccard(domain, cand)
        if jaccard >= TOKEN_JACCARD_THRESHOLD:
            score = jaccard * 0.9  # slightly penalize vs containment
            if score > best_score:
                best_score = score
                best_candidate = cand
                best_reason = f"token_jaccard({jaccard:.2f})"

    # 3. Embedding similarity (expensive, only if no good name match found)
    if best_score < 0.7 and text:
        emb_sim = _embedding_similarity(domain, domain)
        if emb_sim is not None and emb_sim < EMBEDDING_THRESHOLD:
            # Would need embedding server — skip for now, name matching is sufficient
            pass

    if best_candidate and best_score >= TOKEN_JACCARD_THRESHOLD:
        logger.info(f"Domain gate: {domain} -> {best_candidate} ({best_reason})")
        return (best_candidate, best_reason, best_score)

    # No match found — allow as provisional
    logger.info(f"Domain gate: {domain} allowed as provisional (no canonical match)")
    return (domain, "provisional", 0.0)


def record_redirect(old_domain: str, new_domain: str, reason: str):
    """Record a domain redirect in the redirects table for audit trail."""
    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS domain_redirects (
            old_domain TEXT NOT NULL,
            new_domain TEXT NOT NULL,
            reason TEXT,
            created_at REAL DEFAULT (strftime('%s','now')),
            UNIQUE(old_domain)
        )""")
        c.execute("""INSERT OR REPLACE INTO domain_redirects
                     (old_domain, new_domain, reason) VALUES (?, ?, ?)""",
                  (old_domain, new_domain, reason))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to record redirect: {e}")


def check_promotion(domain: str) -> str:
    """Check if a provisional domain should be promoted.

    Promotion criteria:
      - >= 5 experiments
      - >= 7 days since first experiment
      - NOT a craft domain (reserved for observation)

    Returns: 'promoted', 'candidate', or 'provisional'
    """
    CRAFT_DOMAINS = {'coppersmithing', 'saddlery', 'woodcarving', 'pottery',
                     'tanning', 'cobbling', 'millinery', 'ceramics'}
    if domain in CRAFT_DOMAINS:
        return 'provisional'  # observation period

    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("""SELECT COUNT(*) FROM experiments
                     WHERE domain = ? AND typeof(created_at) = 'real'""", (domain,))
        count = c.fetchone()[0]

        c.execute("""SELECT MIN(CAST(created_at AS REAL)) FROM experiments
                     WHERE domain = ? AND typeof(created_at) = 'real'""", (domain,))
        first_exp = c.fetchone()[0]

        if first_exp is None:
            conn.close()
            return 'provisional'

        import time
        now = time.time()
        days_old = (now - first_exp) / 86400

        if count >= 5 and days_old >= 7:
            # Promote to canonical!
            c.execute("""CREATE TABLE IF NOT EXISTS domain_promotions (
                domain TEXT PRIMARY KEY, status TEXT DEFAULT 'provisional',
                first_seen REAL, experiment_count INTEGER DEFAULT 0,
                last_growth_at REAL, promotion_eligible_at REAL,
                promoted_at REAL, notes TEXT)""")
            c.execute("""INSERT OR REPLACE INTO domain_promotions
                         (domain, status, experiment_count, promoted_at, notes)
                         VALUES (?, 'canonical', ?, ?, ?)""",
                      (domain, count, now, f'Auto-promoted: {count} exps, {days_old:.0f}d'))
            conn.commit()
            conn.close()
            logger.info(f"Domain promoted: {domain} ({count} exps, {days_old:.0f}d)")
            # Invalidate canonical cache
            global CanonicalCache
            CanonicalCache = None
            return 'promoted'
        elif count >= 3:
            conn.close()
            return 'candidate'
        else:
            conn.close()
            return 'provisional'
    except Exception as e:
        logger.warning(f"Promotion check failed for {domain}: {e}")
        return 'provisional'


def get_redirect(old_domain: str) -> Optional[str]:
    """Look up if a domain has been redirected."""
    try:
        conn = _get_db()
        c = conn.cursor()
        c.execute("SELECT new_domain FROM domain_redirects WHERE old_domain = ?",
                  (old_domain,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def run_merge_sweep(dry_run: bool = True) -> Dict:
    """Analyze and optionally execute domain merges for all small domains.

    Returns summary dict with counts of merges, provisional domains, etc.
    """
    import time

    canonical = get_canonical_domains()
    conn = _get_db()
    c = conn.cursor()

    # Get all small domains
    c.execute("""SELECT domain, COUNT(*) as cnt
                 FROM experiments WHERE typeof(created_at) = 'real'
                 GROUP BY domain HAVING cnt < 10 ORDER BY cnt""")
    small_domains = c.fetchall()

    merges = []       # (old_domain, new_domain, reason, exp_count)
    provisional = []  # (domain, exp_count) — no canonical match found
    already_redirected = []

    for domain, count in small_domains:
        # Check if already redirected
        existing = get_redirect(domain)
        if existing:
            already_redirected.append((domain, existing))
            continue

        resolved, reason, conf = resolve_domain(domain)
        if resolved != domain and reason != "provisional":
            merges.append((domain, resolved, reason, count))
        else:
            provisional.append((domain, count))

    if not dry_run:
        # Execute merges
        import time as _time
        now = _time.time()
        for old_domain, new_domain, reason, count in merges:
            # Update experiments table
            c.execute("UPDATE experiments SET domain = ? WHERE domain = ?",
                      (new_domain, old_domain))
            # Record redirect
            c.execute("""CREATE TABLE IF NOT EXISTS domain_redirects (
                old_domain TEXT NOT NULL, new_domain TEXT NOT NULL,
                reason TEXT, created_at REAL DEFAULT (strftime('%s','now')),
                UNIQUE(old_domain))""")
            c.execute("""INSERT OR REPLACE INTO domain_redirects
                         (old_domain, new_domain, reason, created_at)
                         VALUES (?, ?, ?, ?)""",
                      (old_domain, new_domain, reason, now))
            # Update domains table — delete old entry (target may already exist)
            c.execute("DELETE FROM domains WHERE name = ?", (old_domain,))
        conn.commit()

    conn.close()

    return {
        "total_small": len(small_domains),
        "merges": merges,
        "provisional": provisional,
        "already_redirected": already_redirected,
        "merge_count": len(merges),
        "provisional_count": len(provisional),
    }


if __name__ == "__main__":
    import json
    import sys

    # Prevent overlapping runs via exclusive flock (same pattern as sync_curiosity_views.py)
    _lock_path = os.path.join(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
                              ".domain_gate_merge.lock")
    _lock_fd = open(_lock_path, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # Another instance is running — exit silently

    try:
        dry = "--apply" not in sys.argv
        result = run_merge_sweep(dry_run=dry)

        if dry:
            print("DRY RUN — no changes applied. Use --apply to execute.")
        else:
            print("MERGES APPLIED.")

        print(f"\nSmall domains: {result['total_small']}")
        print(f"Merges {'would be' if dry else ''} applied: {result['merge_count']}")
        print(f"Provisional (no match): {result['provisional_count']}")
        print(f"Already redirected: {len(result['already_redirected'])}")

        if result['merges']:
            print(f"\n{'='*60}")
            print("MERGE PLAN:")
            print(f"{'='*60}")
            for old, new, reason, cnt in result['merges']:
                print(f"  {old} -> {new}  ({cnt} exps, {reason})")

        if result['provisional']:
            print(f"\n{'='*60}")
            print("PROVISIONAL (no canonical match found):")
            print(f"{'='*60}")
            for domain, cnt in result['provisional']:
                print(f"  {domain}: {cnt} exps")
    finally:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
