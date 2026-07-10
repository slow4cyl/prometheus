#!/usr/bin/env python3
"""Safely repair historical rows stranded in lossy domain mega-buckets.

A retired scheduled writer repeatedly forced unrelated rows into calibration,
injection_detection, safety, and meta_analysis.  The earlier one-shot repaired
experiments only and owns ``reclassify_buckets_undo``.  This separate backfill
covers both historical writer tables without touching that existing rollback
ledger.

The embedding classifier is the source of truth.  A row moves only when its
best canonical target is at least 0.50 similarity and leads its current bucket
by at least 0.15.  Every applied move is reversible in a table-qualified undo
ledger; rollback never overwrites a later correction.

Usage:
    HERMES_HOME=~/.hermes python3 backfill_lossy_domain_merges.py
    HERMES_HOME=~/.hermes python3 backfill_lossy_domain_merges.py --apply
    HERMES_HOME=~/.hermes python3 backfill_lossy_domain_merges.py --rollback
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from typing import Iterable, Mapping, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_retry import get_db
from embedding_domain_classifier import (
    DOMAIN_DESCRIPTIONS,
    cosine_similarity,
    get_embeddings_batch,
    load_domain_embeddings,
)

BUCKETS = frozenset({"calibration", "injection_detection", "safety", "meta_analysis"})
CONFIDENCE = 0.50
MARGIN = 0.15
BATCH_SIZE = 256
UNDO_TABLE = "lossy_domain_backfill_undo_v2"
_ALLOWED_TABLES = frozenset({"experiments", "worker_results"})


def canonical_target_domains() -> set[str]:
    """Return the audited, hand-curated target vocabulary.

    Auto-generated domain descriptions are useful for exploratory routing but
    are not provenance-safe backfill targets: they are derived from the very
    historical labels being repaired and can turn a bulk repair into a second
    uncontrolled taxonomy rewrite.
    """
    return set(DOMAIN_DESCRIPTIONS) - {"general"}


def _placeholders(values: Iterable[object]) -> str:
    return ",".join("?" for _ in values)


def collect_candidates(conn, buckets: Iterable[str] = BUCKETS) -> list[dict]:
    """Return rows from every table the retired writer rewrote.

    Worker findings inherit their experiment hypothesis where available so the
    classifier sees the same context as the experiment-side repair.
    """
    buckets = tuple(buckets)
    if not buckets:
        return []
    placeholders = _placeholders(buckets)
    rows: list[dict] = []
    for row in conn.execute(
        f"""
        SELECT 'experiments' AS table_name, id AS row_id, domain,
               TRIM(SUBSTR(COALESCE(hypothesis, ''), 1, 800) || ' ' ||
                    SUBSTR(COALESCE(result, ''), 1, 800) || ' ' ||
                    SUBSTR(COALESCE((SELECT COALESCE(NULLIF(wr.key_finding,''), NULLIF(wr.finding,''))
                                       FROM worker_results wr
                                      WHERE wr.experiment_id = experiments.id
                                        AND COALESCE(NULLIF(wr.key_finding,''), NULLIF(wr.finding,'')) IS NOT NULL
                                   ORDER BY wr.confidence DESC, wr.created_at DESC LIMIT 1), ''), 1, 400)) AS text
          FROM experiments
         WHERE domain IN ({placeholders})
        """,
        buckets,
    ).fetchall():
        rows.append(dict(row))
    for row in conn.execute(
        f"""
        SELECT 'worker_results' AS table_name, wr.id AS row_id, wr.domain,
               TRIM(SUBSTR(COALESCE(e.hypothesis, ''), 1, 800) || ' ' ||
                    SUBSTR(COALESCE(wr.finding, ''), 1, 800)) AS text
          FROM worker_results AS wr
          LEFT JOIN experiments AS e ON e.id = wr.experiment_id
         WHERE wr.domain IN ({placeholders})
        """,
        buckets,
    ).fetchall():
        rows.append(dict(row))
    return rows


def _similarity_maps(texts: Sequence[str]) -> list[dict[str, float] | None]:
    """Embed a batch and return canonical-domain similarity maps per row."""
    domain_embeddings = {
        domain: embedding
        for domain, embedding in load_domain_embeddings().items()
        if domain != "general"
    }
    embeddings = get_embeddings_batch(list(texts))
    if not embeddings:
        embeddings = [None] * len(texts)
    # Per-row fallback: if the batch dropped a row (oversize/400 against the
    # ~1024-token embed window), retry it alone at a hard 1200-char cap so a
    # single long row can't blank a whole chunk — and so any residual skip is
    # visible, never silent.
    for i, emb in enumerate(embeddings):
        if emb is None and (texts[i] or "").strip():
            solo = get_embeddings_batch([str(texts[i])[:1200]])
            if solo and solo[0] is not None:
                embeddings[i] = solo[0]
    output: list[dict[str, float] | None] = []
    for embedding in embeddings:
        if embedding is None:
            output.append(None)
            continue
        output.append(
            {
                domain: cosine_similarity(embedding, domain_embedding)
                for domain, domain_embedding in domain_embeddings.items()
            }
        )
    if len(output) != len(texts):
        raise RuntimeError(
            f"embedding backend returned {len(output)} rows for {len(texts)} inputs; refusing partial plan"
        )
    return output


def plan_moves(
    rows: Sequence[Mapping[str, object]],
    similarities: Sequence[Mapping[str, float] | None],
    canonical_domains: set[str],
) -> list[tuple[str, str, str, str, float]]:
    """Make only confident, material corrections from precomputed similarities."""
    if len(rows) != len(similarities):
        raise ValueError("rows and similarity maps must have equal length")
    moves: list[tuple[str, str, str, str, float]] = []
    for row, similarity_map in zip(rows, similarities):
        if similarity_map is None or not similarity_map:
            continue
        current = str(row["domain"])
        target = max(similarity_map, key=lambda domain: similarity_map[domain])
        target_score = similarity_map[target]
        # The original repair only trusts a hand-curated *top* match.  Picking
        # the best allowed runner-up would convert an uncertain auto-label
        # into a fabricated correction.
        if target not in canonical_domains or target == "general":
            continue
        current_score = similarity_map.get(current, 0.0)
        if target != current and target_score >= CONFIDENCE and target_score - current_score >= MARGIN:
            table_name = str(row["table_name"])
            if table_name not in _ALLOWED_TABLES:
                raise ValueError(f"unsafe table name from candidate row: {table_name!r}")
            moves.append((table_name, str(row["row_id"]), current, target, target_score))
    return moves


def _ensure_undo_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {UNDO_TABLE} (
            table_name TEXT NOT NULL CHECK(table_name IN ('experiments', 'worker_results')),
            row_id TEXT NOT NULL,
            old_domain TEXT NOT NULL,
            new_domain TEXT NOT NULL,
            applied_at REAL NOT NULL,
            PRIMARY KEY (table_name, row_id)
        )
        """
    )
    existing = conn.execute(f"SELECT COUNT(*) FROM {UNDO_TABLE}").fetchone()[0]
    if existing:
        raise RuntimeError(
            f"{UNDO_TABLE} already contains {existing} rows; rollback or archive that explicit run before applying another"
        )


def apply_moves(conn, moves: Sequence[tuple[str, str, str, str, float]], *, timestamp: float | None = None) -> int:
    """Apply a planned batch atomically and record only rows actually updated."""
    _ensure_undo_table(conn)
    # This script owns its connection, but tests and one-off callers can have
    # already staged rows.  Close that setup transaction before taking the
    # explicit writer lock that makes the update+undo pair atomic.
    conn.commit()
    timestamp = time.time() if timestamp is None else timestamp
    applied = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table_name, row_id, old_domain, new_domain, _score in moves:
            if table_name not in _ALLOWED_TABLES:
                raise ValueError(f"unsafe table name: {table_name!r}")
            cursor = conn.execute(
                f"UPDATE {table_name} SET domain=? WHERE id=? AND domain=?",
                (new_domain, row_id, old_domain),
            )
            if cursor.rowcount != 1:
                continue
            conn.execute(
                f"INSERT INTO {UNDO_TABLE} (table_name, row_id, old_domain, new_domain, applied_at) VALUES (?,?,?,?,?)",
                (table_name, row_id, old_domain, new_domain, timestamp),
            )
            applied += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return applied


def rollback_moves(conn) -> tuple[int, int]:
    """Restore only rows still holding this backfill's target domain."""
    try:
        rows = conn.execute(
            f"SELECT table_name, row_id, old_domain, new_domain FROM {UNDO_TABLE} ORDER BY table_name, row_id"
        ).fetchall()
    except Exception:
        return (0, 0)
    restored = skipped = 0
    # See apply_moves: this operation owns the connection and must begin from
    # a clean boundary so its guarded restores are one atomic ledger pass.
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            table_name, row_id, old_domain, new_domain = row
            if table_name not in _ALLOWED_TABLES:
                raise ValueError(f"unsafe table name in undo ledger: {table_name!r}")
            cursor = conn.execute(
                f"UPDATE {table_name} SET domain=? WHERE id=? AND domain=?",
                (old_domain, row_id, new_domain),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    f"DELETE FROM {UNDO_TABLE} WHERE table_name=? AND row_id=?",
                    (table_name, row_id),
                )
                restored += 1
            else:
                skipped += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return restored, skipped


def _plan_live_moves(rows: Sequence[Mapping[str, object]]) -> list[tuple[str, str, str, str, float]]:
    all_moves: list[tuple[str, str, str, str, float]] = []
    canonical = canonical_target_domains()
    for start in range(0, len(rows), BATCH_SIZE):
        chunk = rows[start : start + BATCH_SIZE]
        maps = _similarity_maps([str(row["text"] or "") for row in chunk])
        all_moves.extend(plan_moves(chunk, maps, canonical))
        print(f"  ...{min(start + len(chunk), len(rows))}/{len(rows)}; moves: {len(all_moves)}", end="\r")
    print()
    return all_moves


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply the planned corrections")
    parser.add_argument("--rollback", action="store_true", help="reverse only this script's still-current changes")
    args = parser.parse_args()
    if args.apply and args.rollback:
        parser.error("--apply and --rollback are mutually exclusive")

    if args.rollback:
        conn = get_db()
        try:
            restored, skipped = rollback_moves(conn)
        finally:
            conn.close()
        print(f"rollback: restored={restored}, retained_later_corrections={skipped}")
        return 0

    conn = get_db(readonly=True)
    try:
        rows = collect_candidates(conn)
    finally:
        conn.close()
    print(f"examining {len(rows)} rows across experiments + worker_results in {sorted(BUCKETS)}")
    moves = _plan_live_moves(rows)
    grouped = Counter((table_name, old, new) for table_name, _id, old, new, _score in moves)
    print(f"would reclassify {len(moves)} rows:")
    for (table_name, old, new), count in grouped.most_common():
        print(f"  {table_name:15s} {old:22s} -> {new:22s} {count}")
    if not args.apply:
        print("DRY-RUN — no writes. Re-run with --apply after a DB backup.")
        return 0

    conn = get_db()
    try:
        applied = apply_moves(conn, moves)
    finally:
        conn.close()
    print(f"applied {applied}/{len(moves)} planned corrections; undo ledger: {UNDO_TABLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
