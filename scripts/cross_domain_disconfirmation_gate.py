#!/usr/bin/env python3
"""cross_domain_disconfirmation_gate.py — don't let an out-of-domain failure alone demote.

Measured asymmetry (architecture-map, "Data conventions"): cross-domain evidence is
EQUALLY reliable for CONFIRMATION but ~6.9pp WORSE for DISCONFIRMATION. That is why the
symmetric weighted_support_count discount was (correctly) rejected — a symmetric weight
would wrongly penalize cross-domain confirmations too. The asymmetric case was left
unbuilt: cross-domain evidence should count for LESS only when it DISPUTES a claim.

Mutating the maturity recompute to reweight is risky (the core derives status every 15m
and the cross-domain flag is not wired into the contradiction basis). So this lane builds
the epistemically-equivalent NON-DESTRUCTIVE version: a corroboration gate. It finds
claims whose refuting evidence is ONLY cross-domain (no same-domain refutation) and are
currently DISPUTED — i.e. demoted purely by out-of-domain evidence — and enqueues an
IN-DOMAIN confirmation re-test. The claim's status is never touched here; the test's
outcome flows through the normal trichotomy:

  SURVIVED in-domain ⇒ the cross-domain dispute does not reproduce where the claim lives:
                       an in-domain support result the normal recompute will credit,
                       letting a weakly-disputed claim recover.
  BROKEN   in-domain ⇒ the dispute is corroborated in-domain: it stands, now on solid
                       (same-domain) ground.
  NARROWED           ⇒ the claim holds in a regime the cross-domain evidence missed.

So a cross-domain disconfirmation no longer stands UNCORROBORATED — it must survive an
in-domain check, which is exactly "discount cross-domain evidence for disconfirmation"
expressed as a gate instead of a weight.

Same machinery as the other probes: own ledger (survives the orphan sweep), --claim
bypass + HYPOTHESIS re-embed hash-collides the result back onto the same claim.
hypothesis_text untouched; no posterior/status written here. Timestamps epoch float.

Usage:
    python3 cross_domain_disconfirmation_gate.py --dry-run
    python3 cross_domain_disconfirmation_gate.py --apply --limit 5
"""
import argparse
import os
import re
import subprocess
import sqlite3
import sys
import time

DB = os.path.expanduser('~/.hermes/prometheus.db')
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ENQUEUER = os.path.join(SCRIPTS, 'adversarial_replication_enqueuer.py')

MAX_GATE = 5   # cap per run — each spawns an in-domain experiment


def ensure_ledger(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS xdomain_disconfirm_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL UNIQUE,
            domain TEXT,
            n_cross_refute INTEGER,
            kanban_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',   -- enqueued|enqueue_failed
            created_at REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_xddc_claim "
                 "ON xdomain_disconfirm_checks(claim_id)")
    conn.commit()


def already_checked(conn, claim_id):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='xdomain_disconfirm_checks'").fetchone():
        return False
    return conn.execute("SELECT 1 FROM xdomain_disconfirm_checks WHERE claim_id=? "
                        "LIMIT 1", (claim_id,)).fetchone() is not None


def get_targets(conn, limit):
    """DISPUTED claims whose refuting evidence is ONLY cross-domain (cross-domain
    refute rows present, same-domain refute rows absent) — demoted purely on
    out-of-domain evidence. Highest cross-refute count first (most in need of an
    in-domain check), then by support so the most-established are corroborated first."""
    rows = conn.execute("""
        WITH ref AS (
            SELECT claim_id,
                   SUM(CASE WHEN COALESCE(is_cross_domain,0)=1 THEN 1 ELSE 0 END) AS xref,
                   SUM(CASE WHEN COALESCE(is_cross_domain,0)=0 THEN 1 ELSE 0 END) AS sref
            FROM claim_evidence WHERE evidence_type='refute' GROUP BY claim_id)
        SELECT kc.id, kc.domain, kc.hypothesis_text, kc.claim_summary, ref.xref,
               COALESCE(kc.weighted_support_count,0) AS wsc
        FROM ref JOIN knowledge_claims kc ON kc.id=ref.claim_id
        WHERE ref.xref>0 AND ref.sref=0
          AND kc.claim_status='DISPUTED'
          AND COALESCE(kc.is_meta,0)=0
        ORDER BY ref.xref DESC, CAST(kc.weighted_support_count AS REAL) DESC
    """).fetchall()
    out = []
    for r in rows:
        if already_checked(conn, r[0]):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def build_note(claim_id, domain, n_cross):
    dom = domain or "its own domain"
    return (
        f"IN-DOMAIN CORROBORATION OF A CROSS-DOMAIN DISPUTE. Claim #{claim_id} is currently "
        f"DISPUTED, but every refutation on record comes from OTHER domains ({n_cross} "
        f"cross-domain refuting result(s)); there is no same-domain refutation. Cross-domain "
        f"evidence is measurably less reliable for disconfirmation than for confirmation, so "
        f"this dispute is not yet corroborated where the claim actually lives.\n"
        f"Re-run the claim's CORE experiment IN ITS OWN DOMAIN ({dom}) and decide whether it "
        f"holds THERE. Do not test it in a foreign domain — the point is the home-domain result.\n"
        f"Report ATTACK_OUTCOME: SURVIVED if the claim holds in-domain (the cross-domain "
        f"dispute does not reproduce where it lives); BROKEN if it fails in-domain too (the "
        f"dispute is corroborated and stands); NARROWED if it holds only under conditions the "
        f"cross-domain evidence did not cover (state them)."
    )


def main():
    ap = argparse.ArgumentParser(
        description="Corroborate cross-domain-only disputes with an in-domain re-test")
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=MAX_GATE)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(f'file:{DB}{"" if apply else "?mode=ro"}', uri=True, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    if apply:
        ensure_ledger(conn)

    targets = get_targets(conn, args.limit)
    if not targets:
        print("No cross-domain-only DISPUTED claims awaiting corroboration.")
        conn.close()
        return 0

    done = 0
    for r in targets:
        cid, domain, hypo, summary, n_cross = r[0], r[1], r[2], r[3], r[4]
        label = (summary or hypo or '')[:90]
        if not apply:
            print(f"[DRY-RUN] would corroborate #{cid} [{domain}] ({n_cross} cross-domain "
                  f"refutes, 0 in-domain)\n    {label}")
            done += 1
            continue
        note = build_note(cid, domain, n_cross)
        res = subprocess.run([sys.executable, ENQUEUER, '--claim', str(cid), '--note', note],
                             capture_output=True, text=True, timeout=180)
        task = re.search(r't_[a-f0-9]+', res.stdout or '')
        ok = res.returncode == 0 and 'already has a live attack' not in (res.stdout or '')
        conn.execute(
            "INSERT INTO xdomain_disconfirm_checks (claim_id, domain, n_cross_refute, "
            "kanban_task_id, status, created_at) VALUES (?,?,?,?,?,?)",
            (cid, domain, n_cross, task.group(0) if task else None,
             'enqueued' if ok else 'enqueue_failed', time.time()))
        conn.commit()
        done += 1
        print(f"{'CORROBORATING' if ok else 'FAILED'} #{cid} [{domain}] in-domain "
              f"({task.group(0) if task else 'no-task'})")
        if not ok:
            print(f"    enqueuer: {(res.stdout or '')[-160:]} {(res.stderr or '')[-120:]}")

    conn.close()
    print(f"\n{'would corroborate' if not apply else 'corroborated'} {done} cross-domain-only "
          f"dispute(s)")
    return 0


if __name__ == '__main__':
    main()
