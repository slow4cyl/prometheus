#!/usr/bin/env python3
"""One-shot forced reclassification of the lossy-merge mega-buckets.

For ~a month a stale domain-normalization copy (normalize_all_domains
SEMANTIC_MERGES, since removed) bulk-UPDATE'd experiments into a few
mega-buckets ('general'->'calibration', 'rag*'->'injection_detection',
'defense'/'attack'->'safety', 'synthesis'/'meta'->'meta_analysis', ...). The
cron reclassifier only touches ORPHAN domains (< min size), so these large
buckets never get re-examined. This runs the embedding classifier (the
system's source of truth) over exactly those buckets and reassigns any
experiment whose embedding CONFIDENTLY disagrees with its current label,
using the SAME >=0.50 / margin rule as reclassify_domains.

Every change is recorded old->new in a sidecar (reclassify_buckets_undo) for
exact rollback. Batched; dry-run by default.

    python3 reclassify_merged_buckets.py            # dry-run: report only
    python3 reclassify_merged_buckets.py --apply
    python3 reclassify_merged_buckets.py --rollback
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prometheus_paths import PROMETHEUS_DB
from embedding_domain_classifier import (
    DOMAIN_DESCRIPTIONS, get_embeddings_batch, load_domain_embeddings,
    cosine_similarity,
)

BUCKETS = ["calibration", "injection_detection", "safety", "meta_analysis"]
CONF = 0.50        # embedding-confident threshold (matches reclassify_domains)
MARGIN = 0.15      # min lead over current-label similarity to move
BATCH = 256


def _classify(texts):
    """-> list of (best_domain, best_sim, cur_sim_lookup) ready per-row."""
    dom_embs = {d: e for d, e in load_domain_embeddings().items() if d != "general"}
    embs = get_embeddings_batch(texts)
    out = []
    for emb in embs:
        if emb is None:
            out.append(None)
            continue
        sims = sorted(((d, cosine_similarity(emb, de)) for d, de in dom_embs.items()),
                      key=lambda x: x[1], reverse=True)
        out.append({d: s for d, s in sims})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    if args.rollback:
        conn = sqlite3.connect(PROMETHEUS_DB, timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
        try:
            rows = conn.execute(
                "SELECT exp_id, old_domain FROM reclassify_buckets_undo").fetchall()
        except sqlite3.OperationalError:
            print("no undo sidecar — nothing to roll back")
            return
        for i in range(0, len(rows), 5000):
            conn.executemany("UPDATE experiments SET domain=? WHERE id=?",
                             [(od, eid) for eid, od in rows[i:i + 5000]])
            conn.commit()
        conn.execute("DROP TABLE reclassify_buckets_undo")
        conn.commit()
        conn.close()
        print(f"rolled back {len(rows)} reclassifications")
        return

    ro = sqlite3.connect(f"file:{PROMETHEUS_DB}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    rows = ro.execute(
        "SELECT id, domain, hypothesis, result FROM experiments "
        "WHERE domain IN ({})".format(",".join("?" * len(BUCKETS))), BUCKETS).fetchall()
    ro.close()
    print(f"examining {len(rows)} experiments in {BUCKETS}")

    canonical = set(DOMAIN_DESCRIPTIONS) - {"general"}
    moves = []          # (exp_id, old, new, sim)
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        texts = [((r["hypothesis"] or "")[:300] + " " + (r["result"] or "")[:300]).strip()
                 for r in chunk]
        sims = _classify(texts)
        for r, sim_map in zip(chunk, sims):
            if not sim_map:
                continue
            best = max(sim_map, key=sim_map.get)
            best_sim = sim_map[best]
            cur_sim = sim_map.get(r["domain"], 0.0)
            if (best != r["domain"] and best in canonical
                    and best_sim >= CONF and (best_sim - cur_sim) >= MARGIN):
                moves.append((r["id"], r["domain"], best, round(best_sim, 3)))
        print(f"  ...{min(i + BATCH, len(rows))}/{len(rows)}  moves so far: {len(moves)}",
              end="\r")
    print()

    from collections import Counter
    by = Counter((o, n) for _, o, n, _ in moves)
    print(f"\nwould reassign {len(moves)} of {len(rows)} experiments:")
    for (o, n), c in by.most_common(20):
        print(f"  {o:20s} -> {n:20s} {c}")

    if not args.apply:
        print("\nDRY-RUN — no writes. Re-run with --apply.")
        return

    conn = sqlite3.connect(PROMETHEUS_DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("DROP TABLE IF EXISTS reclassify_buckets_undo")
    conn.execute("CREATE TABLE reclassify_buckets_undo "
                 "(exp_id TEXT PRIMARY KEY, old_domain TEXT, new_domain TEXT, at REAL)")
    now = time.time()
    for i in range(0, len(moves), 5000):
        chunk = moves[i:i + 5000]
        conn.executemany("INSERT OR IGNORE INTO reclassify_buckets_undo VALUES (?,?,?,?)",
                         [(eid, o, n, now) for eid, o, n, _ in chunk])
        conn.executemany("UPDATE experiments SET domain=? WHERE id=?",
                         [(n, eid) for eid, o, n, _ in chunk])
        conn.commit()
    conn.close()
    print(f"\nreassigned {len(moves)} experiments (rollback: --rollback)")


if __name__ == "__main__":
    main()
