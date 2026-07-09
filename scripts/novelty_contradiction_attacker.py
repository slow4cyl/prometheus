#!/usr/bin/env python3
"""novelty_contradiction_attacker.py — turn a literature contradiction into a real test.

The novelty audit lane (novelty_audit.py) labels a promoted claim CONTRADICTED when
published work disagrees with the claim's CORE assertion. But a literature verdict is
metadata, not evidence: by standing rule a paper can NEVER demote a claim — only a
computational refutation (an attack that reports ATTACK_OUTCOME: BROKEN) can. So today
a CONTRADICTED audit is a dead end: claim 56817 sits ESTABLISHED with a novelty audit
saying the published masses disagree, and nothing ever re-derives it. Coincidental
attacks from other lanes don't count — the contradiction must be the thing that gets
tested.

This lane closes that gap with the SAME machinery the meta-claim prober uses — no edit
to the enqueuer core:

  * find promoted claims (is_meta=0) with a CONTRADICTED novelty audit and no attack
    already spawned FROM that contradiction;
  * enqueue an adversarial replication via adversarial_replication_enqueuer.py --claim
    (whose --claim path bypasses the is_meta / REPLICATED filters), with a note that
    hands the worker the literature's specific disagreement and asks it to RE-DERIVE
    the core result directly and report the trichotomy.

Attach-back is automatic: the attack card re-embeds the claim's HYPOTHESIS line, so the
worker result hash-collides onto the SAME claim through attach_experiment, and
ATTACK_OUTCOME routes through the normal trichotomy — BROKEN⇒the literature was right and
the claim is demoted, NARROWED⇒the claim holds only in a regime the paper didn't cover,
SURVIVED⇒the computation upholds the claim despite the paper (the contradiction was a
search artefact or a peripheral-number confusion).

Ledger: contradiction_attacks records (claim, audit, task) for dedup + observability;
the attack itself lives in adversarial_replications.

Timestamps epoch float (time.time()); never ISO. hypothesis_text never touched.

Usage:
    python3 novelty_contradiction_attacker.py --dry-run
    python3 novelty_contradiction_attacker.py --apply --limit 10
"""
import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import re
import subprocess
import sqlite3
import sys
import time

DB = _PP_PROMETHEUS_DB
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ENQUEUER = os.path.join(SCRIPTS, 'adversarial_replication_enqueuer.py')

MAX_ATTACK = 10   # cap per run — CONTRADICTED is rare, so this is usually a no-op


def ensure_ledger(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contradiction_attacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            novelty_audit_id INTEGER,
            kanban_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',   -- enqueued|enqueue_failed
            created_at REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ca_claim "
                 "ON contradiction_attacks(claim_id)")
    conn.commit()


def already_attacked(conn, claim_id):
    # ledger may not exist yet on a read-only dry-run (created under --apply)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='contradiction_attacks'").fetchone():
        return False
    return conn.execute("SELECT 1 FROM contradiction_attacks WHERE claim_id=? "
                        "LIMIT 1", (claim_id,)).fetchone() is not None


def get_targets(conn, limit):
    """Promoted, still-standing claims with a CONTRADICTED novelty audit and no
    contradiction-attack yet. Newest audit first — a fresh contradiction is the
    most worth testing. Join carries the audit's explanation + citation so the
    note can hand the worker the SPECIFIC disagreement, not just a flag."""
    rows = conn.execute("""
        SELECT na.id AS audit_id, na.claim_id, na.explanation, na.citations,
               na.novel_residue, kc.hypothesis_text, kc.claim_summary,
               kc.domain, kc.claim_status
        FROM novelty_audits na
        JOIN knowledge_claims kc ON kc.id = na.claim_id
        WHERE na.verdict = 'CONTRADICTED'
          AND COALESCE(kc.is_meta, 0) = 0
          AND kc.claim_status NOT IN ('RETIRED','DISPUTED','REFUTED')
        ORDER BY CAST(na.created_at AS REAL) DESC
    """).fetchall()
    out = []
    seen = set()
    for r in rows:
        cid = r[1]
        if cid in seen or already_attacked(conn, cid):
            continue
        seen.add(cid)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _first_citation(citations_json):
    try:
        cites = json.loads(citations_json) if citations_json else []
    except (ValueError, TypeError):
        return ""
    if cites and isinstance(cites, list) and isinstance(cites[0], dict):
        return str(cites[0].get('ref', ''))[:300]
    return ""


def build_note(claim_id, explanation, citation, residue):
    lit = (explanation or "").strip()[:700]
    cite = _first_citation(citation) if not isinstance(citation, str) else citation
    cite_line = f"\nThe disagreeing work: {cite}" if cite else ""
    res_line = f"\nSpecifically contested: {str(residue)[:300]}" if residue else ""
    return (
        f"LITERATURE-CONTRADICTION RE-TEST. This is claim #{claim_id}. A novelty audit "
        f"found that PUBLISHED work disagrees with this claim's core assertion:\n"
        f"\"{lit}\"{cite_line}{res_line}\n"
        f"A paper cannot demote a claim — only your computation can. Do NOT defer to the "
        f"literature. RE-DERIVE the core result directly from first principles / a real "
        f"experiment, then decide who is right.\n"
        f"Report ATTACK_OUTCOME: BROKEN if your own computation contradicts the claim "
        f"(the literature is right and this claim is wrong); NARROWED if the claim holds "
        f"only under conditions the disagreeing work did not cover (state them); SURVIVED "
        f"if your computation upholds the claim despite the published disagreement (say "
        f"why the literature appears to conflict — different regime, or a peripheral "
        f"number vs the core result)."
    )


def main():
    ap = argparse.ArgumentParser(
        description="Convert CONTRADICTED novelty audits into computational attacks")
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=MAX_ATTACK)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(f'file:{DB}{"" if apply else "?mode=ro"}', uri=True, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    if apply:
        ensure_ledger(conn)

    targets = get_targets(conn, args.limit)
    if not targets:
        print("No un-attacked CONTRADICTED claims.")
        conn.close()
        return 0

    done = 0
    for r in targets:
        audit_id, cid, expl, cites, residue = r[0], r[1], r[2], r[3], r[4]
        label = (r[6] or r[5] or '')[:90]
        if not apply:
            print(f"[DRY-RUN] would attack claim #{cid} [{r[8]}] on literature "
                  f"contradiction\n    {label}\n    lit: {(expl or '')[:120]}")
            done += 1
            continue
        note = build_note(cid, expl, cites, residue)
        res = subprocess.run(
            [sys.executable, ENQUEUER, '--claim', str(cid), '--note', note],
            capture_output=True, text=True, timeout=180)
        task = re.search(r't_[a-f0-9]+', res.stdout or '')
        ok = res.returncode == 0 and 'already has a live attack' not in (res.stdout or '')
        conn.execute(
            "INSERT INTO contradiction_attacks (claim_id, novelty_audit_id, "
            "kanban_task_id, status, created_at) VALUES (?,?,?,?,?)",
            (cid, audit_id, task.group(0) if task else None,
             'enqueued' if ok else 'enqueue_failed', time.time()))
        conn.commit()
        done += 1
        print(f"{'ATTACKED' if ok else 'FAILED'} claim #{cid} on literature "
              f"contradiction ({task.group(0) if task else 'no-task'})")
        if not ok:
            print(f"    enqueuer: {(res.stdout or '')[-160:]} {(res.stderr or '')[-120:]}")

    conn.close()
    print(f"\n{'would attack' if not apply else 'attacked'} {done} contradicted claim(s)")
    return 0


if __name__ == '__main__':
    main()
