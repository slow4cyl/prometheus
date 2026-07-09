#!/usr/bin/env python3
"""claim_similarity.py — flag near-DUPLICATE claims (between-claim redundancy).

The independence gate dedupes SUPPORTS within a claim; nothing noticed that two
CLAIMS can be the same finding measured twice. Because a claim's identity is its
hypothesis_text hash, two questions that phrase the same discovery differently
(#65514 "does Ef interact with structural context" vs #65536 "does solvent
accessibility modulate Ef effects" — same protein-stability physics) mint two
claims, both promote, and both can sit in the shelf top-10 as one discovery
double-counted. The compression-synthesis lane clusters mature claims but only to
spawn UNIFY questions — it never flags the pair for merge/cross-link on the shelf.

This is that missing pass: embed every promotion-band claim (Qwen3-Embedding-0.6B, the same
server the RAG uses) and flag semantically near-identical pairs. It is REPORT-ONLY
and POSTERIOR-NEUTRAL — it writes the `claim_near_duplicates` ledger and surfaces
on the discoveries plate; it never merges, demotes, or retracts (a merge is an
operator decision, and two "duplicates" can legitimately hold different verdicts —
#65514 REGIME_SPLIT vs #65536 B_CORRECT — which is itself worth seeing).

Lexical TF-IDF does NOT work here: the summaries are verdict-prose ("ARBITRATION_
VERDICT: B_CORRECT. Discriminating test: ..."), so the finding semantics are
swamped. #65514/#65536 score 0.15 lexically, ~0.75 embedded. Embeddings are
required; the leg fails soft (embed server down → no ledger write, callers proceed).

Cron: `claim-similarity` 15m — a free local scan, so it runs at the epistemic-
maintenance cadence (the box does ~150 exp/hr; the shelf must not rot on a slow
timer). Thresholds are MODEL-SPECIFIC — re-run `--calibrate` and re-tune after any
embed-model swap (cosine geometry shifts hard between models). CLI:
    python3 claim_similarity.py --report           # compute, print, write ledger
    python3 claim_similarity.py --report --dry-run # print only, no write
    python3 claim_similarity.py --calibrate        # distribution + top pairs, no write (re-tune)
    python3 claim_similarity.py --top 30           # show more pairs
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Thresholds are MODEL-SPECIFIC — cosine geometry shifts hard with the embedding
# model. Re-tuned 2026-07-05 for the 1024-d server model (was 0.86/0.82 for 768-d
# nomic, whose whole distribution sat ~0.4 higher). Validated against the same known
# duplicates: breakdown-point #63396/#63400 (0.90), expertise-fragility trio (0.83-
# 0.88), DESI/JWST/p-value clusters (0.79-0.86); #65514/#65536 (Ef/solvent, related-
# not-dup) lands at 0.74 = 'related'. All-pairs p99=0.57, so 0.78 is the extreme tail
# (~p99.97). After ANY embed-model swap, run `--calibrate` and re-tune these two.
DUP_THRESHOLD = 0.78       # near-identical — one discovery double-counted (merge candidate)
RELATED_THRESHOLD = 0.72   # strongly related — worth a cross-link, maybe distinct

_VERDICT_PROSE = re.compile(
    r"(ARBITRATION_VERDICT:|ATTACK_OUTCOME:)\s*[A-Z_]+\.?", re.I)


def _clean_summary(s):
    """Strip the leading verdict token so finding semantics dominate the embedding."""
    return _VERDICT_PROSE.sub("", s or "").strip()


def fetch_claims(conn):
    """Promotion-band, science-shelf claims (meta + empirical-fact excluded — the
    same _NONSCI filter the health board and discovery candidates use)."""
    return conn.execute(
        """SELECT id, claim_status, hypothesis_text,
                  COALESCE(NULLIF(claim_summary, ''), hypothesis_text) AS summ
           FROM knowledge_claims
           WHERE claim_status IN ('REPLICATED', 'ESTABLISHED')
             AND COALESCE(is_empirical_fact, 0) = 0
             AND COALESCE(is_meta, 0) = 0""").fetchall()


def embed_claims(rows):
    """(ids, unit-normalized embedding matrix) via the Qwen3 embedding server, or (ids, None)
    on any failure — the leg must never sink the cron."""
    try:
        import numpy as np
        import experiment_rag as er
    except Exception:
        return [r["id"] for r in rows], None
    ids, texts = [], []
    for r in rows:
        ids.append(r["id"])
        texts.append(((r["hypothesis_text"] or "") + " . "
                      + _clean_summary(r["summ"])[:600])[:2000])
    try:
        E = np.asarray(er.get_embeddings(texts), dtype=float)
        if E.ndim != 2 or E.shape[0] != len(ids):
            return ids, None
        E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-9)
        return ids, E
    except Exception:
        return ids, None


def find_pairs(ids, E, rows_by_id, floor=RELATED_THRESHOLD):
    """All claim pairs with cosine >= floor, most-similar first."""
    import numpy as np
    S = E @ E.T
    pairs = []
    n = len(ids)
    for i in range(n):
        # upper triangle only
        js = np.where(S[i, i + 1:] >= floor)[0]
        for jj in js:
            j = i + 1 + int(jj)
            a, b = ids[i], ids[j]
            sim = float(S[i, j])
            ra, rb = rows_by_id[a], rows_by_id[b]
            pairs.append({
                "a": a, "b": b, "sim": sim,
                "tier": "duplicate" if sim >= DUP_THRESHOLD else "related",
                "status_a": ra["claim_status"], "status_b": rb["claim_status"],
                "both_established": ra["claim_status"] == "ESTABLISHED"
                                   and rb["claim_status"] == "ESTABLISHED",
            })
    pairs.sort(key=lambda p: -p["sim"])
    return pairs


def calibrate(ids, E, rows_by_id, top=30):
    """Print the cosine distribution + top pairs so the two thresholds can be re-tuned
    after an embed-model swap (models pack cosines very differently — the 768-d nomic (now Qwen3-1024)
    sat ~0.4 higher than the 1024-d successor). No writes."""
    import numpy as np
    S = E @ E.T
    iu = np.triu_indices(len(ids), k=1)
    vals = S[iu]
    print(f"embedded {len(ids)} claims | dim={E.shape[1]} | pairs={len(vals)}")
    print("all-pairs cosine percentiles:")
    for p in (50, 75, 90, 95, 99, 99.5, 99.9):
        print(f"   p{p}: {np.percentile(vals, p):.3f}")
    print(f"   max={vals.max():.3f}  mean={vals.mean():.3f}")
    print("pairs at candidate thresholds:")
    for t in (0.90, 0.85, 0.82, 0.80, 0.78, 0.75, 0.72, 0.70):
        print(f"   >= {t}: {int((vals >= t).sum())}")
    print(f"current DUP_THRESHOLD={DUP_THRESHOLD}  RELATED_THRESHOLD={RELATED_THRESHOLD}")
    order = np.argsort(-vals)[:top]
    print(f"top {top} pairs:")
    for k in order:
        a, b = ids[iu[0][k]], ids[iu[1][k]]
        print(f"   {vals[k]:.3f}  #{a}/#{b}: "
              f"{(rows_by_id[a]['hypothesis_text'] or '')[:44]}  ||  "
              f"{(rows_by_id[b]['hypothesis_text'] or '')[:44]}")


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claim_near_duplicates (
            claim_a INTEGER NOT NULL,
            claim_b INTEGER NOT NULL,
            similarity REAL NOT NULL,
            tier TEXT NOT NULL,
            status_a TEXT, status_b TEXT,
            detected_at REAL NOT NULL,
            PRIMARY KEY (claim_a, claim_b))""")
    conn.commit()


def write_ledger(conn, pairs):
    """Full refresh (the table is tiny): a pair drops out the moment either claim
    is demoted, merged, or re-tiered off the science shelf. One short transaction."""
    ensure_table(conn)
    now = time.time()
    conn.execute("DELETE FROM claim_near_duplicates")
    conn.executemany(
        "INSERT INTO claim_near_duplicates "
        "(claim_a, claim_b, similarity, tier, status_a, status_b, detected_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [(p["a"], p["b"], round(p["sim"], 4), p["tier"],
          p["status_a"], p["status_b"], now) for p in pairs])
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Flag near-duplicate promotion-band claims")
    ap.add_argument("--report", action="store_true", help="compute, print, write ledger")
    ap.add_argument("--dry-run", action="store_true", help="with --report: print only, no write")
    ap.add_argument("--calibrate", action="store_true",
                    help="print cosine distribution + top pairs, no write (re-tune thresholds)")
    ap.add_argument("--top", type=int, default=20, help="how many pairs to print")
    args = ap.parse_args()

    from db_retry import get_db
    conn = get_db()
    conn.row_factory = __import__("sqlite3").Row
    rows = fetch_claims(conn)
    rows_by_id = {r["id"]: r for r in rows}
    ids, E = embed_claims(rows)
    if E is None:
        print("embed server unavailable — no similarity computed (fail-soft, ledger untouched).")
        conn.close()
        return 0
    if args.calibrate:
        calibrate(ids, E, rows_by_id, top=max(args.top, 30))
        conn.close()
        return 0
    pairs = find_pairs(ids, E, rows_by_id)
    dups = [p for p in pairs if p["tier"] == "duplicate"]
    print(f"{len(rows)} promotion-band claims · {len(dups)} near-duplicate pairs "
          f"(>= {DUP_THRESHOLD}) · {len(pairs) - len(dups)} related (>= {RELATED_THRESHOLD})")
    for p in pairs[:args.top]:
        star = " ★both-ESTABLISHED" if p["both_established"] else ""
        print(f"  {p['sim']:.3f} [{p['tier']:9s}] #{p['a']} ({p['status_a'][:4]}) / "
              f"#{p['b']} ({p['status_b'][:4]}){star}")
        for cid in (p["a"], p["b"]):
            print(f"        #{cid}: {(rows_by_id[cid]['hypothesis_text'] or '')[:78]}")
    if args.report and not args.dry_run:
        write_ledger(conn, pairs)
        print(f"\nwrote {len(pairs)} pairs to claim_near_duplicates ledger.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
