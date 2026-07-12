#!/usr/bin/env python3
"""
compression_synthesis.py — the COMPRESSION PHASE (option C, fully autonomous).

Prometheus is built as an EXPLORATION engine (maximizes coverage: novelty,
diversity, domain-expansion, boundary-breaking). It is excellent at finding NEW
things and blind to the fact that many of them are the SAME thing. This script
is the missing second half of the loop. It runs as a SEPARATE periodic pass (no
scoring term competing in the thermostat — that would make the system fight
itself) that:

  1. DISTILL : embed every accumulated ESTABLISHED/REPLICATED claim with the live
               embedding server, cluster the embeddings WITHOUT any predefined
               labels, and treat each cluster as a candidate "dependency
               bottleneck" (a mechanism the search keeps rediscovering). Fan-out
               = how many DISTINCT domains a cluster spans. High fan-out + deep
               cross-domain spread = a real bottleneck. Auto-label each cluster
               from its most central claims (no human keyword list anywhere).
  2. FEED BACK : for each solid bottleneck, inject ONE unifying question back into
               the curiosity queue ("is there one rule behind all N of these?").
  3. HUNT EXCEPTIONS : for each bottleneck, find domains where a DISPUTED claim
               sits inside the same cluster and inject a boundary question
               ("why does it break there?").

The handoff is ONE-WAY: questions are written into `curiosities` exactly like
synthesis does; the existing scorer picks them up. Bottlenecks are written to
`compression_bottlenecks`. Nothing here touches portfolio weights.

Discovery is SELF-DEFINING: clusters are found by the embedding geometry, not by
keywords. The system can surface hallways nobody told it to look for.

Companion fix: generate_synthesis_body.py — the drift monitor no longer treats
converging diversity as failure while a compression pass is active.

Usage:
    python3 compression_synthesis.py            # dry run, no writes
    python3 compression_synthesis.py --apply    # write table + inject curiosities
    python3 compression_synthesis.py --json
"""
import argparse
import json
import os
import sys
import time
import fcntl
import collections

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from db_retry import get_db  # noqa: E402

LOCK_PATH = os.path.expanduser("~/.hermes/.compression_synthesis.lock")

# --- Tunables -------------------------------------------------------------
MAX_CLAIMS = 50000         # process the FULL eligible set (EST/REPL/DISPUTED with
                           # text — currently ~12K). Measured: ~81s end-to-end for
                           # 12K (68s embed on GPU + 12s CPU cluster, 0.5GB matrix).
                           # Headroom to 50K so it stays full-coverage as claims grow;
                           # AgglomerativeClustering is O(n^2) memory (~9.5GB at 50K,
                           # still fine). Revisit only if eligible claims approach 50K.
MIN_DOMAINS = 8            # a cluster must span >= this many domains to count
INJECT_MIN_DOMAINS = 12    # only inject unifying Qs for clusters this broad
MIN_CLUSTER_SIZE = 6       # ignore tiny clusters (noise)
DISTANCE_THRESHOLD = 0.30  # cosine-distance cut (empirically tuned: 0.30 yields
                           # ~300 coherent clusters / ~30 real bottlenecks; >=0.40
                           # collapses everything into one useless mega-blob)
MAX_BLOB_FRACTION = 0.40   # reject any cluster holding >40% of all claims — that's
                           # a catch-all, not a mechanism (defends against blob drift)
MAX_UNIFYING_INJECT = 12
MAX_BOUNDARY_INJECT = 12
INJECT_PRIORITY = 2
SOURCE_TAG = "compression_synthesis"

# Stopwords for auto-labeling cluster mechanisms from claim text.
_STOP = set("""the a an of to in is do does how that this and or for it they when
where with within across via using used use can could would should may might
between among into onto from at on by as be been being are was were has have had
than then thus so if but not no yes we our their there here which who whom whose
what why over under above below up down out off again further more most some any
each other such only own same very will just because about against during before
after while above does doc claim claims finding findings experiment experiments
result results hypothesis mechanism domain system effect""".split())


def _norm(v):
    import numpy as np
    v = np.asarray(v, dtype="float32")
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def fetch_claims(conn):
    # Full eligible set: every ESTABLISHED/REPLICATED/DISPUTED claim with usable
    # text. No sampling — the compressor must see the whole map to find every
    # cross-domain bottleneck. LIMIT is a safety ceiling only (see MAX_CLAIMS).
    #
    # 2026-06-25: ingest VERDICT-DIRECTION and DISAGREEMENT signal alongside the
    # text. Previously fetch_claims pulled only text+domain+status, so the
    # clustering geometry and the question generator were blind to whether the
    # claims in a cluster actually AGREE — two claims that flatly contradict each
    # other on a number were pulled into proximity and reported as mutual support.
    # knowledge_claims has no mechanism_type/hypothesis_supported (those live on
    # worker_results), but it DOES carry the signal we need: support/refute/
    # contradiction counts (verdict direction), claim_type (DIRECTIONAL vs
    # NON_DIRECTIONAL), and spurious_agreement (false-consensus score). We attach
    # these so the question generator can surface disagreement instead of
    # presupposing unity. first_experiment_id is pulled for the lineage fix
    # (exact-key parent resolution, replacing the fragile 60-char text prefix).
    rows = conn.execute("""
        SELECT id, domain, claim_status, claim_summary, hypothesis_text, last_updated_at,
               support_count, refute_count, contradiction_count, claim_type,
               spurious_agreement, first_experiment_id,
               transfer_survival_score
        FROM knowledge_claims
        WHERE claim_status IN ('ESTABLISHED','REPLICATED','DISPUTED')
          AND (claim_summary IS NOT NULL OR hypothesis_text IS NOT NULL)
        LIMIT ?
    """, (MAX_CLAIMS,)).fetchall()
    claims = []
    for r in rows:
        text = ((r["claim_summary"] or "") + " " + (r["hypothesis_text"] or "")).strip()
        if len(text) < 12:
            continue
        sup = int(r["support_count"] or 0)
        ref = int(r["refute_count"] or 0)
        claims.append({
            "id": r["id"], "domain": r["domain"] or "?",
            "status": r["claim_status"], "text": text,
            # verdict direction + disagreement signal (2026-06-25)
            "support_count": sup,
            "refute_count": ref,
            "net_support": sup - ref,
            "contradiction_count": int(r["contradiction_count"] or 0),
            "claim_type": r["claim_type"] or "",
            "spurious_agreement": float(r["spurious_agreement"] or 0.0),
            "first_experiment_id": r["first_experiment_id"],
            "transfer_survival_score": r["transfer_survival_score"],
        })
    return claims


def embed_all(texts):
    """Batch-embed via the live embedding server (auto-detects ONNX/Qwen3).
    The embedding client prints backend chatter to stdout; redirect that to
    stderr so --json output stays clean for the cron wrapper to parse."""
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        from embedding_domain_classifier import get_embeddings_batch
        out = []
        B = 128
        for i in range(0, len(texts), B):
            out.extend(get_embeddings_batch(texts[i:i + B]))
    return out


def cluster(vectors):
    """Self-defining clustering: no labels, no preset cluster count.
    Agglomerative with a cosine-distance threshold — the geometry decides how
    many mechanisms exist."""
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering
    X = np.vstack([_norm(v) for v in vectors])
    model = AgglomerativeClustering(
        n_clusters=None, metric="cosine", linkage="average",
        distance_threshold=DISTANCE_THRESHOLD,
    )
    return model.fit_predict(X), X


def auto_label(texts):
    """Name a cluster from the words that recur across its claims (tf-style).
    No predefined vocabulary — the label is whatever the cluster is about."""
    import re
    counts = collections.Counter()
    for t in texts:
        words = set(w for w in re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", t.lower())
                    if w not in _STOP)
        counts.update(words)
    top = [w for w, _ in counts.most_common(4)]
    return "/".join(top) if top else "unlabeled-mechanism"


def distill(conn):
    claims = fetch_claims(conn)
    if len(claims) < MIN_CLUSTER_SIZE:
        return [], {"claims": len(claims), "embedded": 0, "clusters": 0}

    vecs = embed_all([c["text"] for c in claims])
    # Drop claims whose embedding failed (server hiccup) — keep alignment.
    kept = [(c, v) for c, v in zip(claims, vecs) if v]
    if len(kept) < MIN_CLUSTER_SIZE:
        return [], {"claims": len(claims), "embedded": len(kept), "clusters": 0}
    claims = [c for c, _ in kept]
    vecs = [v for _, v in kept]

    labels, X = cluster(vecs)

    # Group claim indices by cluster id.
    groups = collections.defaultdict(list)
    for idx, lab in enumerate(labels):
        groups[int(lab)].append(idx)

    import numpy as np
    bottlenecks = []
    n_total = len(claims)
    for lab, idxs in groups.items():
        if len(idxs) < MIN_CLUSTER_SIZE:
            continue
        # Blob guard: a cluster holding a huge fraction of ALL claims is a
        # catch-all, not a mechanism. Skip it (defends against threshold drift).
        if len(idxs) > MAX_BLOB_FRACTION * n_total:
            continue
        domains = collections.Counter(claims[i]["domain"] for i in idxs
                                      if claims[i]["status"] != "DISPUTED")
        if len(domains) < MIN_DOMAINS:
            continue
        exceptions = collections.Counter(
            claims[i]["domain"] for i in idxs if claims[i]["status"] == "DISPUTED")

        # Centroid → most representative claims (for labeling + the question).
        sub = X[idxs]
        centroid = sub.mean(axis=0)
        centroid /= (np.linalg.norm(centroid) or 1)
        sims = sub @ centroid
        order = np.argsort(-sims)
        central_idxs = [idxs[o] for o in order[:8]]
        central_texts = [claims[i]["text"] for i in central_idxs]
        exemplar = central_texts[0][:200]
        exemplar_first_exp = claims[central_idxs[0]].get("first_experiment_id")

        # DISAGREEMENT signal across the cluster (2026-06-25): the cluster is the
        # set of claims the search keeps rediscovering, but proximity is NOT
        # agreement. Surface how many members actually conflict so the question
        # generator can ask about the disagreement instead of presupposing unity.
        n_disputed = sum(1 for i in idxs if claims[i]["status"] == "DISPUTED")
        n_net_negative = sum(1 for i in idxs if claims[i].get("net_support", 0) < 0)
        n_high_sa = sum(1 for i in idxs if claims[i].get("spurious_agreement", 0.0) >= 0.6)
        n_nondirectional = sum(1 for i in idxs
                               if (claims[i].get("claim_type") or "").upper() == "NON_DIRECTIONAL")
        # Transfer survival: how many claims in this cluster have a
        # transfer_survival_score? Threshold at 0.60 (25th percentile of
        # transfer-positive claims, measured 2026-06-26).
        TSS_THRESHOLD = 0.60
        tss_scores = [claims[i].get("transfer_survival_score") for i in idxs
                      if claims[i].get("transfer_survival_score") is not None]
        n_transfer_proven = sum(1 for s in tss_scores if s >= TSS_THRESHOLD)
        n_transfer_failed = sum(1 for s in tss_scores if s < TSS_THRESHOLD)
        n_no_tss = len(idxs) - len(tss_scores)
        has_transfer_signal = bool(tss_scores)
        # A cluster "contains disagreement" if a meaningful share of its members
        # are disputed, net-negative, or flagged false-consensus.
        has_disagreement = bool(n_disputed or n_net_negative or n_high_sa)

        bottlenecks.append({
            "mechanism": auto_label(central_texts),
            "fan_out_domains": len(domains),
            "claim_count": sum(domains.values()),
            "domains": dict(domains.most_common()),
            "exceptions": dict(exceptions.most_common()),
            "exemplar": exemplar,
            "exemplar_first_exp": exemplar_first_exp,
            # disagreement metrics (2026-06-25)
            "cluster_size": len(idxs),
            "n_disputed": n_disputed,
            "n_net_negative": n_net_negative,
            "n_high_sa": n_high_sa,
            "n_nondirectional": n_nondirectional,
            "has_disagreement": has_disagreement,
            # transfer survival metrics (2026-06-26)
            "n_transfer_proven": n_transfer_proven,
            "n_transfer_failed": n_transfer_failed,
            "n_no_tss": n_no_tss,
            "has_transfer_signal": has_transfer_signal,
        })

    bottlenecks.sort(key=lambda b: (b["fan_out_domains"], b["claim_count"]), reverse=True)
    stats = {"claims": len(claims), "embedded": len(vecs), "clusters": len(groups)}
    return bottlenecks, stats


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compression_bottlenecks (
            id              INTEGER PRIMARY KEY,
            mechanism       TEXT NOT NULL,
            fan_out_domains INTEGER NOT NULL,
            claim_count     INTEGER NOT NULL,
            domains_json    TEXT,
            exception_json  TEXT,
            exemplar        TEXT,
            unifying_q      TEXT,
            injected        INTEGER DEFAULT 0,
            created_at      REAL NOT NULL,
            run_id          TEXT
        )
    """)
    # Older table from the keyword version lacks `exemplar`; add if missing.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(compression_bottlenecks)").fetchall()]
        if "exemplar" not in cols:
            conn.execute("ALTER TABLE compression_bottlenecks ADD COLUMN exemplar TEXT")
    except Exception:
        pass
    # Cluster-signature ledger (2026-07-12): the question TEXT drifts every run
    # (claim_count grows 262->275, keyword order shuffles), so the 70-char-prefix
    # dedup re-injected the SAME cluster every tick — the 'side/arbitration/
    # works/verdict' procedural-artifact cluster was re-investigated 12x in one
    # day, workers correctly concluding ARTIFACT each time with no memory of
    # having asked. One injection per stable signature; re-ask only on MATERIAL
    # growth (see cluster_already_asked).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compression_cluster_ledger (
            signature   TEXT PRIMARY KEY,
            claim_count INTEGER,
            injected_at REAL
        )
    """)
    conn.commit()


def unifying_question(b):
    top = ", ".join(list(b["domains"].keys())[:4])
    # Transfer survival framing (2026-06-26). If the cluster has transfer signal,
    # frame the question around whether the mechanism survived cross-domain testing.
    transfer_context = ""
    if b.get("has_transfer_signal"):
        tp = b.get("n_transfer_proven", 0)
        tf = b.get("n_transfer_failed", 0)
        nt = b.get("n_no_tss", 0)
        if tp > tf:
            transfer_context = (
                f" {tp} of these claims have a transfer_survival_score >= 0.60 "
                f"(the mechanism was tested across domains and held up), "
                f"{tf} scored below 0.60 (failed or weakened under transfer), "
                f"and {nt} have no transfer data."
            )
        elif tf > tp:
            transfer_context = (
                f" Only {tp} of these claims survived cross-domain transfer "
                f"(transfer_survival_score >= 0.60), while {tf} scored below 0.60 — "
                f"the mechanism may be domain-sensitive or unreliable under transfer."
            )
    # 2026-06-25: do NOT presuppose unity when the cluster contains disagreement.
    # The old prompt always asked "Is there ONE underlying rule?", a leading
    # question that came back ~96% CONFIRMED even on clusters partly constituted
    # by contradiction. When the ingested signal shows conflict among the
    # clustered claims (disputed members, net-negative support, or false-consensus
    # flags), ask about the DISAGREEMENT first — that is where the real science is.
    if b.get("has_disagreement"):
        conflict_bits = []
        if b.get("n_disputed"):
            conflict_bits.append(f"{b['n_disputed']} are DISPUTED")
        if b.get("n_net_negative"):
            conflict_bits.append(f"{b['n_net_negative']} have more refuting than supporting evidence")
        if b.get("n_high_sa"):
            conflict_bits.append(f"{b['n_high_sa']} are flagged false-consensus (agree on the verdict flag while disagreeing in substance)")
        conflict = "; ".join(conflict_bits) or "the members do not agree"
        return (f"[COMPRESSION] Semantic clustering grouped {b['claim_count']} claims "
                f"('{b['mechanism']}') across {b['fan_out_domains']} domains "
                f"({top}), but these claims DO NOT all agree: {conflict}.{transfer_context} "
                f"Representative claim: \"{b['exemplar']}\". Proximity in embedding "
                f"space is not agreement. Does this cluster actually describe ONE "
                f"mechanism, or has it merged DISTINCT or CONFLICTING results that "
                f"only look alike? Identify the axis on which the cases disagree "
                f"(e.g. effect direction, magnitude, boundary conditions) and design "
                f"the experiment that determines whether they unify, split into "
                f"families, or directly contradict.")
    # No detected disagreement: still avoid hard-presupposing unity, but the
    # unify-or-split framing is appropriate here.
    return (f"[COMPRESSION] A recurring mechanism ('{b['mechanism']}') was found by "
            f"semantic clustering across {b['fan_out_domains']} independent domains "
            f"({b['claim_count']} claims with no detected internal disagreement; "
            f"e.g. {top}).{transfer_context} Representative claim: \"{b['exemplar']}\". Is there ONE "
            f"underlying rule that predicts when/where this mechanism applies across "
            f"all of them, or do the cases split into distinct families? Design the "
            f"experiment that unifies or splits them.")


def boundary_question(b, exc_domain):
    return (f"[COMPRESSION-BOUNDARY] The '{b['mechanism']}' mechanism holds across "
            f"{b['fan_out_domains']} domains but is DISPUTED in {exc_domain}. Under what "
            f"condition does it BREAK in {exc_domain} specifically — what is different there "
            f"that the other domains lack? That difference bounds the mechanism.")


def already_injected(conn, text):
    row = conn.execute(
        "SELECT 1 FROM curiosities WHERE source_experiment=? AND text LIKE ? LIMIT 1",
        (SOURCE_TAG, text[:70] + "%")).fetchone()
    return row is not None


def cluster_signature(mechanism, suffix=""):
    """Order-stable identity of a cluster: its sorted keyword set. The rendered
    question text is NOT a stable key — counts and keyword order drift run to
    run — so dedup must key on this."""
    core = ",".join(sorted(p.strip() for p in (mechanism or "").split("/") if p.strip()))
    return f"{core}|{suffix}" if suffix else core


def cluster_already_asked(conn, b, suffix=""):
    """One investigation per cluster signature. Re-ask ONLY on material growth
    (claim_count >= 1.5x what it was when first asked) — a drifting count is
    the same question wearing a new number."""
    sig = cluster_signature(b["mechanism"], suffix)
    row = conn.execute(
        "SELECT claim_count FROM compression_cluster_ledger WHERE signature=?",
        (sig,)).fetchone()
    if row is None:
        return False
    return b["claim_count"] < 1.5 * max(1, row[0] or 1)


def inject(conn, text):
    # Generation throttle
    try:
        from generation_throttle import should_throttle
        if should_throttle(conn):
            print(f"  [THROTTLE] Skipping compression injection — cap reached")
            return
    except Exception:
        pass
    
    conn.execute(
        "INSERT INTO curiosities (text, priority, status, source_experiment, created_at) "
        "VALUES (?,?,?,?,?)",
        (text, INJECT_PRIORITY, "active", SOURCE_TAG, time.time()))


def run(apply=False, as_json=False):
    # --- single-instance flock: a run takes ~80s+ and the schedule is 30m, but
    # under load a run can overlap the next tick. Two concurrent runs both try to
    # write -> lock contention -> the intermittent 'error' status. Hold an
    # exclusive lock for the life of this process; if another run holds it, exit
    # cleanly (the next scheduled tick will pick up). ---
    _lockf = open(LOCK_PATH, "w")
    if apply:
        try:
            fcntl.flock(_lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            msg = {"run_id": None, "skipped": "another compression run in progress",
                   "claims_embedded": 0, "clusters_found": 0, "bottlenecks_found": 0,
                   "unifying_questions_injected": 0, "boundary_questions_injected": 0,
                   "applied": False}
            if as_json:
                print(json.dumps(msg, indent=2))
            else:
                print("compression-synthesis: skipped — another run in progress")
            return msg

    conn = get_db()
    # Manual transaction control for the short write phase: put the underlying
    # connection in autocommit mode so our explicit BEGIN IMMEDIATE/COMMIT are the
    # ONLY transaction, and reads before it don't sit inside an implicit txn.
    try:
        conn._conn.isolation_level = None
    except Exception:
        pass
    ensure_table(conn)
    run_id = f"comp_{int(time.time())}"
    # SLOW PHASE (read-only): embed + cluster the whole claim pool. No write txn
    # is open here, so workers are never blocked by the distill step.
    bottlenecks, stats = distill(conn)

    # BUILD PHASE (reads only): decide what to inject + what rows to write, using
    # dedup reads. Accumulate into batches; do NOT write yet, so no write
    # transaction is held open across this loop.
    inj_u = inj_b = 0
    curiosity_rows = []      # (text, priority, status, source, created_at, parent_curiosity_id)
    bottleneck_rows = []     # full INSERT tuples for compression_bottlenecks
    ledger_rows = []         # (signature, claim_count, injected_at)
    for b in bottlenecks:
        uq = unifying_question(b) if b["fan_out_domains"] >= INJECT_MIN_DOMAINS else None
        do_u = bool(apply and uq and inj_u < MAX_UNIFYING_INJECT
                    and not already_injected(conn, uq)
                    and not cluster_already_asked(conn, b))
        # Resolve parent curiosity. 2026-06-25: prefer an EXACT-KEY join over the
        # old 60-char text-prefix match (which failed ~half the time because a
        # claim's text rarely matches a curiosity's text verbatim, leaving the
        # injected question orphaned as a depth-0 root). The exemplar claim carries
        # first_experiment_id; curiosities.resolved_by_experiment is the reverse
        # link, so this is an exact join, not a fuzzy prefix. Fall back to the text
        # prefix only when the claim has no first_experiment_id.
        parent_id = None
        fexp = b.get("exemplar_first_exp")
        if fexp:
            prow = conn.execute(
                "SELECT id FROM curiosities WHERE resolved_by_experiment = ? "
                "ORDER BY id DESC LIMIT 1",
                (fexp,)
            ).fetchone()
            if prow:
                parent_id = prow[0]
        if parent_id is None and b.get("exemplar"):
            prow = conn.execute(
                "SELECT id FROM curiosities WHERE text LIKE ? ORDER BY id DESC LIMIT 1",
                (b["exemplar"][:60] + "%",)
            ).fetchone()
            if prow:
                parent_id = prow[0]
        if do_u:
            curiosity_rows.append((uq, INJECT_PRIORITY, "active", SOURCE_TAG, time.time(), parent_id))
            ledger_rows.append((cluster_signature(b["mechanism"]), b["claim_count"], time.time()))
            inj_u += 1
        if apply and b["exceptions"]:
            for exc in list(b["exceptions"].keys()):
                if inj_b >= MAX_BOUNDARY_INJECT:
                    break
                bq = boundary_question(b, exc)
                if (not already_injected(conn, bq)
                        and not cluster_already_asked(conn, b, suffix=f"boundary:{exc}")):
                    curiosity_rows.append((bq, INJECT_PRIORITY, "active", SOURCE_TAG, time.time(), parent_id))
                    ledger_rows.append((cluster_signature(b["mechanism"], f"boundary:{exc}"),
                                        b["claim_count"], time.time()))
                    inj_b += 1
        if apply:
            bottleneck_rows.append(
                (b["mechanism"], b["fan_out_domains"], b["claim_count"],
                 json.dumps(b["domains"]), json.dumps(b["exceptions"]), b["exemplar"],
                 uq or "", 1 if do_u else 0, time.time(), run_id))

    # WRITE PHASE (short, batched): open the write transaction as late as possible
    # and hold it for the minimum time. BEGIN IMMEDIATE grabs the write lock once;
    # executemany batches all inserts; commit releases immediately. This is the
    # fix for the lock-contention errors — the write window is milliseconds, not
    # the ~80s of distill+build.
    if apply:
        try:
            conn.execute("BEGIN IMMEDIATE")
            if bottleneck_rows:
                conn.executemany(
                    "INSERT INTO compression_bottlenecks (mechanism, fan_out_domains,"
                    " claim_count, domains_json, exception_json, exemplar, unifying_q,"
                    " injected, created_at, run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    bottleneck_rows)
            if curiosity_rows:
                conn.executemany(
                    "INSERT INTO curiosities (text, priority, status, source_experiment,"
                    " created_at, parent_curiosity_id) VALUES (?,?,?,?,?,?)",
                    curiosity_rows)
            if ledger_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO compression_cluster_ledger "
                    "(signature, claim_count, injected_at) VALUES (?,?,?)",
                    ledger_rows)
            conn.commit()
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            try:
                fcntl.flock(_lockf, fcntl.LOCK_UN)
            except Exception:
                pass

    summary = {
        "run_id": run_id, "claims_embedded": stats["embedded"],
        "clusters_found": stats["clusters"], "bottlenecks_found": len(bottlenecks),
        "unifying_questions_injected": inj_u, "boundary_questions_injected": inj_b,
        "applied": apply,
        "top": [{"mechanism": b["mechanism"], "fan_out_domains": b["fan_out_domains"],
                 "claim_count": b["claim_count"], "exceptions": list(b["exceptions"].keys())[:5]}
                for b in bottlenecks[:10]],
    }
    if as_json:
        print(json.dumps(summary, indent=2)); return summary

    mode = "APPLIED" if apply else "DRY RUN (no writes — use --apply)"
    print(f"COMPRESSION SYNTHESIS (semantic) — {mode}")
    print(f"run_id: {run_id}")
    print(f"claims embedded: {stats['embedded']}  raw clusters: {stats['clusters']}  "
          f"bottlenecks (>= {MIN_DOMAINS} domains): {len(bottlenecks)}")
    print(f"injected: {inj_u} unifying + {inj_b} boundary questions")
    print("=" * 74)
    for b in bottlenecks:
        flag = "  <-- HALLWAY (unifying question)" if b["fan_out_domains"] >= INJECT_MIN_DOMAINS else ""
        print(f"\n* [{b['mechanism']}] {b['fan_out_domains']} domains, {b['claim_count']} claims{flag}")
        print("   spans: " + ", ".join(f"{d}({c})" for d, c in list(b["domains"].items())[:6]))
        if b["exceptions"]:
            print("   BOUNDARY (disputed in): " + ", ".join(list(b["exceptions"].keys())[:5]))
        print(f"   exemplar: {b['exemplar'][:140]}")
    return summary


def main():
    ap = argparse.ArgumentParser(description="Compression phase: semantic bottleneck distillation + feedback.")
    ap.add_argument("--apply", action="store_true", help="Write table + inject curiosities. Default: dry run.")
    ap.add_argument("--json", dest="as_json", action="store_true")
    args = ap.parse_args()
    run(apply=args.apply, as_json=args.as_json)


if __name__ == "__main__":
    main()
