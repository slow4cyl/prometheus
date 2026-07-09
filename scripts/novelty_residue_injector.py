#!/usr/bin/env python3
"""novelty_residue_injector.py — turn literature-audit residues into questions.

Closes the audit loop the OTHER direction. novelty_audit.py records, per claim,
what prior work does NOT cover — `novel_residue` on PARTIALLY_KNOWN / NOT_FOUND
verdicts ("the specific ρ=0.807 correlation is not published", "these bond-number
thresholds are novel"). Today that residue is a dead-end note. But a result that
is genuinely NOT in the literature is exactly the result that most needs
independent scrutiny before the shelf trusts it — the audit has located the
system's own frontier.

This injects one follow-up curiosity per un-injected residue, framed in the two
FERTILE question morphologies (variable-mutation + transfer — the templates that
reproduce, per the morphology analysis) so it drives independent reproduction and
boundary-finding rather than terminating:

    [LIT-RESIDUE] Claim #N (<domain>) asserts something prior work does NOT
    establish: <residue>. Independently reproduce this specific result, then find
    the ONE variable whose change breaks it, and test whether it transfers to a
    neighboring domain.

Lineage: the new curiosity attaches to the claim's first experiment (exact key:
first_experiment_id → curiosities.resolved_by_experiment; text-prefix fallback),
matching compression_synthesis's parent resolution. Dedup is exact via a
`residue_injected` flag on novelty_audits (additive migration), not a fuzzy text
LIKE. Timestamps are epoch floats (time.time()); status 'active' so the scorer
folds it into the queue; NEVER ISO, NEVER datetime('now').

Usage:
    python3 novelty_residue_injector.py --dry-run
    python3 novelty_residue_injector.py --apply           # inject, capped
    python3 novelty_residue_injector.py --apply --limit 20
"""
import argparse
import os
import sqlite3
import time

DB = os.path.expanduser('~/.hermes/prometheus.db')
SOURCE_TAG = 'novelty_residue'
INJECT_PRIORITY = 2          # match compression_synthesis's injected-question tier
MIN_RESIDUE_CHARS = 40       # skip trivially short residues
MAX_INJECT = 12              # cap per run — same discipline as the other injectors
# verdicts whose residue represents genuinely-uncovered ground worth re-deriving
RESIDUE_VERDICTS = ('PARTIALLY_KNOWN', 'NOT_FOUND')


def ensure_columns(conn):
    """Additive: a per-audit injected flag for exact, non-fuzzy dedup."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(novelty_audits)")}
    if 'residue_injected' not in cols:
        conn.execute("ALTER TABLE novelty_audits ADD COLUMN residue_injected INTEGER DEFAULT 0")
        conn.commit()


def get_residues(conn, limit):
    """Un-injected residues on still-mature, non-meta claims. Newest audit per
    claim wins (a re-audit supersedes). Ordered by claim weight so the
    highest-value frontier results get re-derived first."""
    placeholders = ','.join('?' * len(RESIDUE_VERDICTS))
    # the dedup column may not exist yet on a read-only dry-run (it is created
    # under --apply); if absent, treat every residue as un-injected.
    have_flag = any(r[1] == 'residue_injected'
                    for r in conn.execute("PRAGMA table_info(novelty_audits)"))
    injected_filter = ("AND COALESCE(na.residue_injected, 0) = 0" if have_flag else "")
    return conn.execute(f"""
        SELECT na.id AS audit_id, na.claim_id, na.verdict, na.novel_residue,
               kc.domain, kc.first_experiment_id, kc.claim_status,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM novelty_audits na
        JOIN knowledge_claims kc ON kc.id = na.claim_id
        WHERE na.verdict IN ({placeholders})
          AND na.novel_residue IS NOT NULL
          AND length(trim(na.novel_residue)) >= ?
          {injected_filter}
          AND COALESCE(kc.is_meta, 0) = 0
          AND kc.claim_status IN ('REPLICATED', 'ESTABLISHED')
          AND na.id = (SELECT MAX(na2.id) FROM novelty_audits na2
                       WHERE na2.claim_id = na.claim_id)
        ORDER BY CAST(kc.weighted_support_count AS REAL) DESC, na.claim_id
        LIMIT ?
    """, (*RESIDUE_VERDICTS, MIN_RESIDUE_CHARS, limit)).fetchall()


def resolve_parent(conn, first_experiment_id):
    """Lineage parent by exact experiment key, then text-prefix fallback — the
    same resolution compression_synthesis uses so residue questions sit in the
    lineage of the claim they scrutinize."""
    if not first_experiment_id:
        return None
    row = conn.execute(
        "SELECT id FROM curiosities WHERE resolved_by_experiment = ? LIMIT 1",
        (first_experiment_id,)).fetchone()
    return row[0] if row else None


def build_question(claim_id, domain, verdict, residue):
    residue = ' '.join(str(residue).split())          # collapse whitespace
    if len(residue) > 300:
        residue = residue[:300].rsplit(' ', 1)[0] + '…'
    dom = f" ({domain})" if domain else ""
    novelty = ("prior work does NOT establish" if verdict == 'PARTIALLY_KNOWN'
               else "the literature does not cover at all")
    return (f"[LIT-RESIDUE] Claim #{claim_id}{dom} asserts something {novelty}: "
            f"{residue} Independently reproduce this specific result, then find the "
            f"ONE variable whose change breaks it, and test whether it transfers to "
            f"a neighboring domain.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--limit', type=int, default=MAX_INJECT)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    mode = '' if apply else '?mode=ro'
    conn = sqlite3.connect(f'file:{DB}{mode}', uri=True, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    if apply:
        ensure_columns(conn)

    residues = get_residues(conn, args.limit)
    if not residues:
        print("No un-injected literature residues on mature claims.")
        conn.close()
        return 0

    injected = 0
    for r in residues:
        audit_id, claim_id, verdict, residue = r[0], r[1], r[2], r[3]
        domain, first_exp = r[4], r[5]
        q = build_question(claim_id, domain, verdict, residue)
        parent = resolve_parent(conn, first_exp) if apply else None
        if not apply:
            print(f"[DRY-RUN] claim #{claim_id} [{verdict}] wsc={r[7]:.1f}\n    {q[:200]}")
            injected += 1
            continue
        # short write txn per residue — never hold the WAL lock across the loop
        conn.execute(
            "INSERT INTO curiosities (text, priority, status, source_experiment, "
            "created_at, parent_curiosity_id) VALUES (?,?,?,?,?,?)",
            (q, INJECT_PRIORITY, 'active', SOURCE_TAG, time.time(), parent))
        conn.execute("UPDATE novelty_audits SET residue_injected = 1 WHERE id = ?",
                     (audit_id,))
        conn.commit()
        injected += 1
        print(f"INJECTED residue question for claim #{claim_id} [{verdict}] "
              f"(parent={parent})")

    conn.close()
    verb = "would inject" if not apply else "injected"
    print(f"\n{verb} {injected} literature-residue question(s)")
    return 0


if __name__ == '__main__':
    main()
