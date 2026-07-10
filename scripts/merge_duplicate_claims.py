#!/usr/bin/env python3
"""Merge hypothesis-fragment duplicate claims (reduced shape, reversible).

The pre-2026-07-04 ``normalize_hypothesis`` did not strip retest/transfer
tags before hashing, so one question re-created itself as many claims as its
trailing "(NN% success, N transfers)" stats varied. Identity was fixed
FORWARD-only (claim_lifecycle.py never re-hashes existing rows), leaving a
historical backlog of fragment groups. This is the opt-in one-shot that
collapses them.

REDUCED SHAPE (deliberate — provenance signals are DERIVED, never ratcheted):
re-point only the live provenance tables the maturity recompute actually reads
(claim_evidence, adversarial_replications, claim_scopes, answer_adjudications,
circularity_reviews, meta_transfer_predictions) from each loser onto its
survivor, tombstone the loser (claim_status='MERGED', merged_into=survivor),
and carry the survivor's claim_hash to the group hash where no member already
owns it (the forward attach point, so future retests stop forking). History
tables (claim_status_history, knowledge_claims_audit_history) are LEFT on the
losers — re-pointing a row's transition history onto the survivor would
fabricate a history it never had. The derived recompute rebuilds the
survivor's tier/wsc/posterior from the re-pointed live evidence.

SURVIVOR RULE: (a) the member already owning the canonical hash (where new
evidence lands) > (b) highest maturity tier > (c) most evidence rows >
(d) oldest / lowest id.

Safety: per-group BEGIN IMMEDIATE with membership re-verification inside the
txn (a group whose hashes changed since planning is skipped and logged);
every mutation writes a row to the claim_merge_undo ledger; --rollback
reverses a run by timestamp. Not a cron job — a supervised one-shot; it also
exceeds the 120s cron budget.

Usage:
    HERMES_HOME=~/.hermes python3 merge_duplicate_claims.py                 # dry-run
    HERMES_HOME=~/.hermes python3 merge_duplicate_claims.py --apply
    HERMES_HOME=~/.hermes python3 merge_duplicate_claims.py --rollback --ts <epoch>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db_retry import get_db
from claim_lifecycle import normalize_hypothesis

UNDO_TABLE = "claim_merge_undo"
# Live provenance tables re-pointed loser->survivor. (table, claim_id_col,
# [unique_partner_cols]) — each partner is a column that, together with the
# claim id, carries a UNIQUE constraint; a loser row that would collide with a
# survivor row on any of them is DELETED (full row to the ledger) rather than
# re-pointed. Re-points are recorded per-ROW (by rowid) so rollback restores
# exactly the moved rows and never disturbs the survivor's own.
REPOINT = [
    ("claim_evidence", "claim_id", ["worker_result_id", "key_finding"]),
    ("claim_scopes", "claim_id", ["source_curiosity_id"]),
    ("adversarial_replications", "claim_id", []),
    ("answer_adjudications", "claim_id", []),
    ("circularity_reviews", "claim_id", []),
    ("meta_transfer_predictions", "meta_claim_id", []),
]
TIER_RANK = {"ESTABLISHED": 5, "REPLICATED": 4, "CANDIDATE": 3,
             "CONDITIONAL": 3, "PROVISIONAL": 3, "DISPUTED": 1, "MERGED": -1}


def _norm_hash(text: str) -> str:
    return hashlib.sha256(normalize_hypothesis(text or "").encode()).hexdigest()[:16]


def _tier_rank(status) -> int:
    if status in TIER_RANK:
        return TIER_RANK[status]
    return 2 if status else 0  # any other non-null legacy tier > NULL


def build_plan(conn) -> dict:
    """Group live claims by recomputed hash; return {group_hash: plan} for
    every group with >1 member. plan = {survivor, losers[], needs_rehash}."""
    rows = conn.execute(
        "SELECT id, claim_hash, hypothesis_text, claim_status, "
        "(SELECT COUNT(*) FROM claim_evidence ce WHERE ce.claim_id = kc.id) AS ev_rows, "
        "created_at FROM knowledge_claims kc "
        "WHERE COALESCE(claim_status,'') != 'MERGED'"
    ).fetchall()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[_norm_hash(r["hypothesis_text"])].append(dict(r))
    plan = {}
    for gh, members in groups.items():
        if len(members) < 2:
            continue
        for m in members:
            m["is_canonical"] = 1 if (m["claim_hash"] == gh) else 0
            try:
                m["created_f"] = float(m["created_at"] or 0)
            except (TypeError, ValueError):
                m["created_f"] = 0.0
        survivor = max(members, key=lambda m: (
            m["is_canonical"], _tier_rank(m["claim_status"]),
            m["ev_rows"], -m["created_f"] if m["created_f"] else 0, -m["id"]))
        losers = [m for m in members if m["id"] != survivor["id"]]
        plan[gh] = {
            "survivor": survivor,
            "loser_ids": [m["id"] for m in losers],
            "member_ids": sorted(m["id"] for m in members),
            "needs_rehash": survivor["claim_hash"] != gh,
        }
    return plan


def _ensure_schema(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")}
    if "merged_into" not in cols:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN merged_into INTEGER")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {UNDO_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            migration_ts REAL NOT NULL, group_hash TEXT NOT NULL,
            action TEXT NOT NULL, tbl TEXT, row_pk INTEGER,
            col TEXT, old_value TEXT, new_value TEXT, full_row_json TEXT)
    """)


def _log(conn, ts, gh, action, *, tbl=None, row_pk=None, col=None,
         old=None, new=None, full=None) -> None:
    conn.execute(
        f"INSERT INTO {UNDO_TABLE} (migration_ts, group_hash, action, tbl, "
        "row_pk, col, old_value, new_value, full_row_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, gh, action, tbl, row_pk, col,
         None if old is None else str(old), None if new is None else str(new), full))


def _repoint_group(conn, ts, gh, survivor_id, loser_ids) -> dict:
    counts = defaultdict(int)
    for loser in loser_ids:
        for tbl, idcol, partners in REPOINT:
            # Delete any loser row that would collide with a survivor row on a
            # UNIQUE(claim_id, partner) — full row to the ledger for restore.
            for partner in partners:
                collide = conn.execute(
                    f"SELECT l.rowid AS rid, l.* FROM {tbl} l WHERE l.{idcol} = ? "
                    f"AND EXISTS (SELECT 1 FROM {tbl} s WHERE s.{idcol} = ? "
                    f"AND s.{partner} IS l.{partner})", (loser, survivor_id)).fetchall()
                for row in collide:
                    d = {k: row[k] for k in row.keys() if k != "rid"}
                    _log(conn, ts, gh, "delete_collision", tbl=tbl, row_pk=row["rid"],
                         full=json.dumps(d, default=str))
                    conn.execute(f"DELETE FROM {tbl} WHERE rowid = ?", (row["rid"],))
                    counts[f"{tbl}:collision_del"] += 1
            # Re-point each surviving loser row BY ROWID so rollback can restore
            # exactly these rows without touching the survivor's own.
            for (rid,) in conn.execute(
                    f"SELECT rowid FROM {tbl} WHERE {idcol} = ?", (loser,)).fetchall():
                conn.execute(f"UPDATE {tbl} SET {idcol} = ? WHERE rowid = ?", (survivor_id, rid))
                _log(conn, ts, gh, "repoint", tbl=tbl, row_pk=rid, col=idcol,
                     old=loser, new=survivor_id)
                counts[f"{tbl}:repoint"] += 1
    return counts


def apply_plan(conn, plan, *, dry_run=True) -> dict:
    ts = time.time()
    if not dry_run:
        _ensure_schema(conn)
        conn.commit()
    stats = defaultdict(int)
    for gh, p in plan.items():
        survivor_id, loser_ids = p["survivor"]["id"], p["loser_ids"]
        if dry_run:
            stats["groups"] += 1
            stats["tombstones"] += len(loser_ids)
            stats["rehash"] += 1 if p["needs_rehash"] else 0
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-verify membership inside the txn: skip a group a live writer
            # changed between planning and now (new member, or a member's hash
            # moved). Prevents merging a claim that just gained/lost identity.
            live = conn.execute(
                "SELECT id, hypothesis_text, claim_hash FROM knowledge_claims "
                f"WHERE id IN ({','.join('?' * len(p['member_ids']))})",
                p["member_ids"]).fetchall()
            if len(live) != len(p["member_ids"]) or any(
                    _norm_hash(r["hypothesis_text"]) != gh for r in live):
                conn.execute("ROLLBACK")
                stats["skipped_changed"] += 1
                continue
            c = _repoint_group(conn, ts, gh, survivor_id, loser_ids)
            for k, v in c.items():
                stats[k] += v
            for loser in loser_ids:
                old = conn.execute(
                    "SELECT claim_status, status, merged_into FROM knowledge_claims WHERE id=?",
                    (loser,)).fetchone()
                _log(conn, ts, gh, "tombstone", tbl="knowledge_claims", row_pk=loser,
                     full=json.dumps({k: old[k] for k in old.keys()}, default=str))
                conn.execute(
                    "UPDATE knowledge_claims SET claim_status='MERGED', status='MERGED', "
                    "merged_into=? WHERE id=?", (survivor_id, loser))
                stats["tombstones"] += 1
            if p["needs_rehash"]:
                old_hash = p["survivor"]["claim_hash"]
                _log(conn, ts, gh, "rehash_survivor", tbl="knowledge_claims",
                     row_pk=survivor_id, col="claim_hash", old=old_hash, new=gh)
                conn.execute("UPDATE knowledge_claims SET claim_hash=? WHERE id=?",
                             (gh, survivor_id))
                stats["rehash"] += 1
            conn.execute("COMMIT")
            stats["groups"] += 1
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            stats["errors"] += 1
            print(f"  group {gh}: ERROR {exc}", file=sys.stderr)
    stats["migration_ts"] = ts
    return dict(stats)


def rollback(conn, ts) -> dict:
    rows = conn.execute(
        f"SELECT * FROM {UNDO_TABLE} WHERE migration_ts = ? ORDER BY id DESC", (ts,)).fetchall()
    if not rows:
        return {"restored": 0, "note": "no ledger rows for that ts"}
    stats = defaultdict(int)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            act = r["action"]
            if act == "repoint":
                conn.execute(f"UPDATE {r['tbl']} SET {r['col']} = ? WHERE rowid = ?",
                             (int(r["old_value"]), r["row_pk"]))
                stats["repoint_reversed"] += 1
            elif act == "tombstone":
                d = json.loads(r["full_row_json"])
                conn.execute("UPDATE knowledge_claims SET claim_status=?, status=?, "
                             "merged_into=? WHERE id=?",
                             (d.get("claim_status"), d.get("status"),
                              d.get("merged_into"), r["row_pk"]))
                stats["tombstone_reversed"] += 1
            elif act == "rehash_survivor":
                conn.execute("UPDATE knowledge_claims SET claim_hash=? WHERE id=?",
                             (r["old_value"], r["row_pk"]))
                stats["rehash_reversed"] += 1
            elif act == "delete_collision":
                d = json.loads(r["full_row_json"])
                cols = ",".join(d.keys())
                conn.execute(f"INSERT INTO {r['tbl']} ({cols}) VALUES ({','.join('?'*len(d))})",
                             list(d.values()))
                stats["collision_restored"] += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--ts", type=float, help="migration_ts to roll back")
    args = ap.parse_args()

    with get_db() as conn:
        conn.row_factory = __import__("sqlite3").Row
        if args.rollback:
            if not args.ts:
                ap.error("--rollback requires --ts")
            print(json.dumps(rollback(conn, args.ts), indent=1))
            return 0
        plan = build_plan(conn)
        n_losers = sum(len(p["loser_ids"]) for p in plan.values())
        n_rehash = sum(1 for p in plan.values() if p["needs_rehash"])
        print(f"plan: {len(plan)} groups, {n_losers} losers to tombstone, "
              f"{n_rehash} survivor rehashes")
        if not args.apply:
            print("DRY-RUN — no writes. Re-run with --apply after a DB backup.")
            return 0
        stats = apply_plan(conn, plan, dry_run=False)
        print(json.dumps(stats, indent=1))
        print(f"undo: {UNDO_TABLE} (migration_ts={stats['migration_ts']}); "
              f"rollback: --rollback --ts {stats['migration_ts']}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
