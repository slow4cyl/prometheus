#!/usr/bin/env python3
"""Auto-merge fragmented domains + auto-extend embedding centroids.

Runs periodically to catch domains that the gate missed or that were
created before the gate. Also calls auto_extend_descriptions() from
embedding_domain_classifier.py to detect new domain labels that lack
embedding centroids and generate them automatically.

Triple purpose:
1. Domain merge: fix stale domain references across prometheus.db and rag.db
   — now syncs BOTH experiments and worker_results tables (previously only
   experiments, leaving malformed domains as ghost nodes in the topology).
2. Discover NEW redirects: calls run_merge_sweep() to find small domains
   that should be merged into canonical parents but don't have a redirect
   entry yet. Also detects canonical-domain duplicates (>= 10 exps each)
   via embedding similarity.
3. Centroid auto-extend: any domain with >=5 experiments that lacks an
   embedding centroid gets one generated from its experiment hypotheses.

Uses flock to prevent overlapping runs and db_retry for safe
database access — same pattern as sync_curiosity_views.py.
"""

import sys, os, fcntl, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_retry import get_db
from domain_creation_gate import get_redirect, record_redirect, resolve_domain, \
    get_canonical_domains, _name_overlap, _token_jaccard, _token_containment
import embedding_domain_classifier as embedding_classifier

HERMES = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
LOCK_PATH = os.path.join(HERMES, ".domain_merge_sync.lock")
PROMETHEUS_DB = os.path.join(HERMES, "prometheus.db")
RAG_DB = os.path.join(HERMES, "rag", "rag.db")

# Domains that are junk sentinels and should never appear as real domains.
JUNK_DOMAINS = {'uncategorized', 'unclassified_pending', 'split_per_experiment', ''}


def _sync_table(conn, table, old_domain, new_domain):
    """Update domain column in a single table. Returns count of rows updated."""
    try:
        cur = conn.execute(
            f"UPDATE {table} SET domain = ? WHERE domain = ?",
            (new_domain, old_domain)
        )
        return cur.rowcount
    except Exception:
        return 0


def _sync_domain_everywhere(conn, old_domain, new_domain):
    """Sync a domain redirect across all tables that have a domain column."""
    total = 0
    for table in ('experiments', 'worker_results'):
        total += _sync_table(conn, table, old_domain, new_domain)

    # Also sync RAG db if it exists
    if os.path.exists(RAG_DB):
        try:
            rag_conn = get_db(RAG_DB)
            for table in ('experiments',):
                _sync_table(rag_conn, table, old_domain, new_domain)
            rag_conn.commit()
            rag_conn.close()
        except Exception:
            pass

    # Delete old entry from domains table if it exists
    try:
        conn.execute("DELETE FROM domains WHERE name = ?", (old_domain,))
    except Exception:
        pass

    return total


def _discover_new_merges(conn):
    """Find small domains (< 10 exps) that should be merged but have no redirect yet.

    Calls resolve_domain() to discover candidates, creates redirect entries,
    then syncs the rows.  This is the proactive discovery loop — previously
    domain_merge_sync only processed domains that ALREADY had redirects,
    so new duplicates accumulated forever.
    """
    c = conn.cursor()

    # Find small domains in experiments that don't have a redirect yet
    c.execute("""SELECT domain, COUNT(*) FROM experiments
                 WHERE domain IS NOT NULL
                 GROUP BY domain HAVING COUNT(*) < 10""")
    small_domains = [(row[0], row[1]) for row in c.fetchall()]

    # Also check worker_results for domains not in experiments at all
    # (orphaned ghost domains from the malformed-concatenation bug)
    c.execute("""SELECT DISTINCT domain FROM worker_results
                 WHERE domain IS NOT NULL
                 AND domain NOT IN (SELECT DISTINCT domain FROM experiments
                                    WHERE domain IS NOT NULL)""")
    orphaned = [(row[0], 0) for row in c.fetchall()]

    merged = 0
    for domain, cnt in small_domains + orphaned:
        if not domain or domain in JUNK_DOMAINS:
            continue
        if get_redirect(domain):
            continue  # already has a redirect

        resolved, reason, conf = resolve_domain(domain)
        if resolved != domain and reason not in ("provisional", "canonical"):
            # Create the redirect and sync everywhere
            record_redirect(domain, resolved, reason)
            updated = _sync_domain_everywhere(conn, domain, resolved)
            if updated:
                print(f"  MERGE: {domain} -> {resolved} ({updated} rows, {reason})")
                merged += 1

    return merged


def _discover_canonical_duplicates(conn):
    """Detect and merge canonical domains (>= 10 exps) that are semantic duplicates.

    The run_merge_sweep in domain_creation_gate only processes domains with
    < 10 experiments, so pairs like multiomics/multi_omics/multi_omics_integration
    (all >= 4 exps, some >= 10) are never compared.  This uses name-overlap,
    token similarity, and embedding cosine distance to find duplicates among
    ALL domains and merges the smaller into the larger.
    """
    c = conn.cursor()
    c.execute("""SELECT domain, COUNT(*) as cnt FROM experiments
                 WHERE domain IS NOT NULL AND domain NOT IN ('uncategorized','unclassified_pending','split_per_experiment')
                 GROUP BY domain HAVING cnt >= 3 ORDER BY cnt DESC""")
    all_domains = [(row[0], row[1]) for row in c.fetchall()]

    # Load embedding centroids once (not per-pair) to avoid O(n^2) file I/O
    import json as _json, math as _math
    centroids = {}
    emb_path = os.path.join(HERMES, "domain_embeddings.json")
    if os.path.exists(emb_path):
        try:
            with open(emb_path) as _f:
                centroids = _json.load(_f)
        except Exception:
            pass

    merged = 0
    seen = set()
    for i, (dom_a, cnt_a) in enumerate(all_domains):
        if dom_a in seen or dom_a in JUNK_DOMAINS:
            continue
        for dom_b, cnt_b in all_domains[i+1:]:
            if dom_b in seen or dom_b in JUNK_DOMAINS:
                continue
            if get_redirect(dom_a) or get_redirect(dom_b):
                continue

            # Determine which is larger (keep the one with more experiments)
            larger, smaller = (dom_a, dom_b) if cnt_a >= cnt_b else (dom_b, dom_a)

            should_merge = False
            reason = ""

            # 1. Stripped-form equality (e.g. multiomics == multi_omics)
            if dom_a.replace('_', '') == dom_b.replace('_', ''):
                should_merge = True
                reason = f"stripped_form_match"

            # 2. High name overlap (substring)
            if not should_merge:
                overlap = _name_overlap(dom_a, dom_b)
                if overlap >= 0.80:
                    should_merge = True
                    reason = f"name_overlap({overlap:.2f})"

            # 3. Token containment: one domain's tokens are a subset of the other's
            if not should_merge:
                tokens_a = set(dom_a.split('_'))
                tokens_b = set(dom_b.split('_'))
                cont_ab = _token_containment(tokens_a, tokens_b)
                cont_ba = _token_containment(tokens_b, tokens_a)
                max_cont = max(cont_ab, cont_ba)
                if max_cont >= 0.80 and min(len(tokens_a), len(tokens_b)) <= 3:
                    should_merge = True
                    reason = f"token_containment({max_cont:.2f})"

            # 4. Embedding similarity (if available)
            if not should_merge and centroids:
                try:
                    if dom_a in centroids and dom_b in centroids:
                        a, b = centroids[dom_a], centroids[dom_b]
                        dot = sum(x*y for x, y in zip(a, b))
                        na = _math.sqrt(sum(x*x for x in a))
                        nb = _math.sqrt(sum(x*x for x in b))
                        cos = dot / (na * nb) if na and nb else 0.0
                        if cos >= 0.92:
                            should_merge = True
                            reason = f"embedding_cosine({cos:.3f})"
                except Exception:
                    pass

            if should_merge:
                record_redirect(smaller, larger, f"canonical_dedup:{reason}")
                updated = _sync_domain_everywhere(conn, smaller, larger)
                if updated:
                    print(f"  DEDUP: {smaller} -> {larger} ({updated} rows, {reason})")
                    merged += 1
                seen.add(smaller)

    return merged


def _cleanup_junk_domains(conn):
    """Reclassify experiments stuck with junk sentinel domains.

    'uncategorized' and 'unclassified_pending' are not real domains — they're
    placeholders from the classifier failing to assign a domain.  Try embedding
    reclassification on the hypothesis text; if that fails, leave them (they'll
    be filtered from the topology by build_topology_export.py).
    """
    c = conn.cursor()
    merged = 0
    for junk in ('uncategorized', 'unclassified_pending'):
        c.execute("SELECT COUNT(*) FROM experiments WHERE domain = ?", (junk,))
        cnt = c.fetchone()[0]
        if cnt == 0:
            continue

        # Try to reclassify each experiment using the embedding classifier
        c.execute("""SELECT id, hypothesis FROM experiments
                     WHERE domain = ? AND hypothesis IS NOT NULL""", (junk,))
        rows = c.fetchall()
        reclassified = 0
        for exp_id, hyp in rows:
            try:
                new_domain = None
                if hasattr(embedding_classifier, 'classify_embedding'):
                    res = embedding_classifier.classify_embedding(hyp[:2000])
                    if isinstance(res, tuple):
                        res = res[0]
                    if res and res not in JUNK_DOMAINS:
                        new_domain = res
                if new_domain:
                    conn.execute("UPDATE experiments SET domain = ? WHERE id = ?",
                                 (new_domain, exp_id))
                    conn.execute("UPDATE worker_results SET domain = ? WHERE experiment_id = ?",
                                 (new_domain, exp_id))
                    reclassified += 1
            except Exception:
                pass

        if reclassified:
            conn.commit()
            print(f"  JUNK CLEANUP: reclassified {reclassified}/{cnt} '{junk}' experiments")
            merged += reclassified

    return merged


def _sync_existing_redirects(conn):
    """Find domains that have a redirect but still have stale rows in any table."""
    c = conn.cursor()

    # Get all redirects
    c.execute("""CREATE TABLE IF NOT EXISTS domain_redirects (
            old_domain TEXT NOT NULL,
            new_domain TEXT NOT NULL,
            reason TEXT,
            created_at REAL DEFAULT (strftime('%s','now')),
            UNIQUE(old_domain)
        )""")
    c.execute("SELECT old_domain, new_domain FROM domain_redirects")
    redirects = c.fetchall()

    fixed = 0
    for old_domain, new_domain in redirects:
        if not old_domain or not new_domain:
            continue
        # Check if any rows still have the old domain
        for table in ('experiments', 'worker_results'):
            c.execute(f"SELECT COUNT(*) FROM {table} WHERE domain = ?", (old_domain,))
            remaining = c.fetchone()[0]
            if remaining > 0:
                _sync_table(conn, table, old_domain, new_domain)
                print(f"  STALE: {old_domain} -> {new_domain} in {table} ({remaining} rows)")
                fixed += remaining

    if fixed:
        conn.commit()

        # Also sync RAG
        if os.path.exists(RAG_DB):
            try:
                rag_conn = get_db(RAG_DB)
                rag_c = rag_conn.cursor()
                for old_domain, new_domain in redirects:
                    if not old_domain or not new_domain:
                        continue
                    rag_c.execute("UPDATE experiments SET domain = ? WHERE domain = ?",
                                  (new_domain, old_domain))
                rag_conn.commit()
                rag_conn.close()
                print("  RAG synced.")
            except Exception:
                pass

    return fixed


def sync():
    """Full sync: process existing redirects, discover new merges, dedup canonicals."""
    conn = get_db(PROMETHEUS_DB)

    total_fixed = 0

    # 1. Process existing redirects (experiments + worker_results)
    fixed = _sync_existing_redirects(conn)
    total_fixed += fixed

    # 2. Discover new merges for small domains
    new_merges = _discover_new_merges(conn)
    total_fixed += new_merges

    # 3. Discover canonical-domain duplicates (>= 10 exps each)
    deduped = _discover_canonical_duplicates(conn)
    total_fixed += deduped

    # 4. Reclassify junk sentinel domains
    junk_fixed = _cleanup_junk_domains(conn)
    total_fixed += junk_fixed

    conn.commit()
    conn.close()

    if total_fixed:
        print(f"Domain sync: {total_fixed} rows fixed "
              f"({fixed} stale, {new_merges} new merges, {deduped} dedups, {junk_fixed} junk reclassified)")


if __name__ == "__main__":
    # Prevent overlapping runs via exclusive flock
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # Another instance is running — exit silently
    try:
        sync()
        # Auto-extend embedding centroids for any new domains
        # that have accumulated enough experiments since last run
        added = embedding_classifier.auto_extend_descriptions(min_experiments=5)
        if added:
            for domain, cnt, preview in added:
                print(f"  New centroid: {domain} ({cnt} exps)")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
