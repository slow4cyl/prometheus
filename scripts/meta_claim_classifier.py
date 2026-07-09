#!/usr/bin/env python3
"""meta_claim_classifier.py — separate claims ABOUT the system from claims about the world.

Why (2026-07-01 audit): the strongest low-spurious-agreement claims were
domain-pair transfer bookkeeping — "[TRANSFER from soil_science] Does soil
science transfer to computer science? (60% success, 30 transfers)" (claim
66949) — i.e. measurements of Prometheus's own machinery ranked alongside
science. Meta-learning is a design goal, so these claims are kept and keep
maturing; they are just tagged is_meta=1 and excluded from the science
leaderboard and external-validity statistics, so "what has the system
discovered" is never answered by self-measurement.

Deliberately high-precision, low-recall: only unambiguous self-reference is
flagged. A worldly-sounding question that happens to involve transfer of a
real mechanism stays is_meta=0 (e.g. claim 3839's "cross-modal transfer rate
between audio and text adversarial detection" reads worldly — its actual
problem is circular construction, which circularity_critic.py handles on a
separate axis).

Usage:
    from meta_claim_classifier import is_meta_claim
    python3 meta_claim_classifier.py --report          # counts + samples, no writes
    python3 meta_claim_classifier.py --backfill        # classify all is_meta IS NULL
    python3 meta_claim_classifier.py --backfill --redo # reclassify everything
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Unambiguous self-reference. Each entry: (compiled pattern, reason).
META_PATTERNS = [
    # Domain-pair transfer-rate bookkeeping: "(60% success, 30 transfers)",
    # with optional REFUTATION-BOOST / HIDDEN-BRIDGE strategy prefix.
    (re.compile(r"\(\s*(?:REFUTATION-BOOST:|HIDDEN-BRIDGE:)?\s*\d+(?:\.\d+)?%\s+success,\s*\d+\s+transfers?", re.I),
     "transfer-rate bookkeeping"),
    # "Does <domain> transfer to <domain>?" — the subject is two domain
    # labels, not a mechanism.
    (re.compile(r"^\s*\[TRANSFER[^\]]*\]\s*Does\s+[\w /-]{2,40}\s+transfer\s+to\s+[\w /-]{2,40}\s*\?", re.I),
     "domain-to-domain transfer question"),
    # Compression/clustering machinery claims ([COMPRESSION], [COMPRESSION-BOUNDARY], ...).
    (re.compile(r"^\s*\[COMPRESSION", re.I), "compression-clustering claim"),
    # The system's own components, by name.
    (re.compile(r"\b(task_refiller|apply_worker_results|write_worker_result|kanban|task card|"
                r"curiosity queue|curiosities table|worker_results|prometheus\.db|self_state|"
                r"quality gate|quality_validator|claim maturity|spurious agreement|"
                r"synthesis (?:worker|merger|cycle|task)|RAG (?:index|dedup)|"
                r"dispatch(?:er)? (?:loop|pipeline)|BENCH3)\b", re.I),
     "names a system component"),
    # Self-measurement of the research loop itself. Generic stats vocabulary
    # ("branching factor", "confirmation rate") is NOT enough — an AST-features
    # claim legitimately uses "branching factor". Require loop context.
    (re.compile(r"\b(experiments per hour|curiosity generation|question generation rate|"
                r"worker confidence cap|"
                r"(?:confirmation|refutation) rate of (?:the )?(?:system|loop|workers?|queue|pipeline)|"
                r"(?:branching factor|depth) of (?:the )?(?:queue|lineages?|curiosit\w+))\b", re.I),
     "measures the research loop"),
]


def is_meta_claim(text):
    """Return (is_meta: bool, reason: str|None) for a claim hypothesis text."""
    if not text:
        return False, None
    for pat, reason in META_PATTERNS:
        if pat.search(text):
            return True, reason
    return False, None


def ensure_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")]
    if "is_meta" not in cols:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN is_meta INTEGER")
        conn.commit()


def backfill(conn, redo=False, batch=2000):
    ensure_column(conn)
    where = "" if redo else "WHERE is_meta IS NULL"
    rows = conn.execute(
        f"SELECT id, hypothesis_text FROM knowledge_claims {where}").fetchall()
    n_meta = n_world = 0
    pending = 0
    for row in rows:
        meta, _ = is_meta_claim(row["hypothesis_text"])
        conn.execute("UPDATE knowledge_claims SET is_meta = ? WHERE id = ?",
                     (1 if meta else 0, row["id"]))
        n_meta += meta
        n_world += (not meta)
        pending += 1
        if pending >= batch:
            conn.commit()
            pending = 0
    conn.commit()
    print(f"Backfilled {len(rows)} claims: {n_meta} meta, {n_world} world")
    return n_meta, n_world


def report(conn):
    ensure_column(conn)
    rows = conn.execute(
        "SELECT id, hypothesis_text FROM knowledge_claims").fetchall()
    metas = []
    reasons = {}
    for row in rows:
        meta, reason = is_meta_claim(row["hypothesis_text"])
        if meta:
            metas.append((row["id"], reason, row["hypothesis_text"]))
            reasons[reason] = reasons.get(reason, 0) + 1
    print(f"{len(metas)} / {len(rows)} claims classify as meta "
          f"({100.0 * len(metas) / max(len(rows), 1):.1f}%)")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {count:>6}  {reason}")
    print("\nSamples:")
    for cid, reason, text in metas[:8]:
        print(f"  #{cid} [{reason}] {(text or '')[:90]}")


if __name__ == "__main__":
    from db_retry import get_db
    ap = argparse.ArgumentParser(description="Tag claims about the system itself (is_meta)")
    ap.add_argument("--report", action="store_true", help="Counts + samples, no writes")
    ap.add_argument("--backfill", action="store_true", help="Classify claims with is_meta IS NULL")
    ap.add_argument("--redo", action="store_true", help="With --backfill: reclassify everything")
    args = ap.parse_args()
    conn = get_db()
    if args.report:
        report(conn)
    elif args.backfill:
        backfill(conn, redo=args.redo)
    else:
        ap.print_help()
    conn.close()
