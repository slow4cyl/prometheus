#!/usr/bin/env python3
"""backfill_claim_summary.py — populate knowledge_claims.claim_summary from the
best supporting worker finding, so claims display their ANSWER instead of the
question that prompted them.

Why (2026-07-04): a claim's headline (`hypothesis_text`) is copied verbatim
from `experiments.hypothesis`, which is the *question* the experiment was set
to answer ("Does X break?"). The `claim_summary` field — which every display
prefers when present — was populated in 13 of 67,609 claims. The actual finding
lives in `claim_evidence.key_finding` ("CONFIRMED: X holds, delta=4.6692…") and
is 99% present. This fills claim_summary from that, non-destructively:
hypothesis_text (which backs claim_hash / grouping identity) is never touched.

The selector picks, per claim, the most representative finding:
  * PROVENANCE FIRST: an arbitration verdict (the adjudicated resolution of a
    dispute) supersedes everything; an adversarial-replication outcome
    (ATTACK_OUTCOME: …) supersedes ordinary findings. This is what keeps a
    NARROWED/regime-split correction from being buried under the confident
    original overclaim it just refuted-in-scope (2026-07-04),
  * then matching the claim's dominant verdict (support if support>=refute, else refute),
  * that is a real conclusion (verdict-prefixed), not janitor/synthesis noise,
  * highest confidence, then most recent (ce.id), then most detailed.
Falls back to any usable finding; leaves claim_summary NULL (=> hypothesis_text
still shows) when a claim genuinely has no finding behind it.

Usage:
    python3 backfill_claim_summary.py --dry-run                # counts only
    python3 backfill_claim_summary.py --sample ESTABLISHED     # before/after
    python3 backfill_claim_summary.py --apply                  # write, batched
    python3 backfill_claim_summary.py --apply --only-empty     # skip already-set
"""
import argparse
import os
import re
import sqlite3
import textwrap
import time

DB = os.path.expanduser('~/.hermes/prometheus.db')

# key_findings that are system/janitor noise, not scientific conclusions
_JUNK_PREFIX = ('JANITOR:', 'AUTO-', 'SKIP', 'MOOT', 'TIMEOUT', 'ERROR',
                'No worker results', 'Report exists', 'Synthesized ')
# worker verdict prefixes — a real conclusion almost always starts with one
_VERDICT = ('CONFIRMED', 'SUPPORTED', 'REFUTED', 'DISCONFIRMED', 'FALSIFIED',
            'PARTIAL', 'NARROWED', 'MIXED', 'INCONCLUSIVE', 'SURVIVED')


def _clean(kf):
    kf = (kf or '').strip().replace('\n', ' ')
    kf = re.sub(r'\s+', ' ', kf)
    if len(kf) < 15:
        return None
    if any(kf.startswith(j) for j in _JUNK_PREFIX):
        return None
    return kf


def _has_verdict(kf):
    up = kf.upper()
    return any(up.startswith(v) for v in _VERDICT)


def _provenance_tier(kf):
    """How much scrutiny stands behind this finding — higher supersedes lower.
    An arbitration verdict is a dispute's adjudicated resolution; an attack
    outcome is the most rigorous test a claim received. Either should headline
    a claim over the confident original it may have narrowed/refuted-in-scope.
    Attack/arbitration findings do NOT start with a _VERDICT word (they lead
    with ATTACK_OUTCOME:/ARBITRATION_VERDICT:), so without this they'd sort
    BELOW an ordinary CONFIRMED — the exact bug that stranded stale summaries."""
    up = kf.upper()
    if up.startswith('ARBITRATION_VERDICT') or up.startswith('ARBITRATION'):
        return 2
    if up.startswith('ATTACK_OUTCOME'):
        return 1
    return 0


def pick_summary(rows, support, refute):
    """rows: list of (evidence_type, confidence, key_finding[, recency_id]).
    Returns a summary string or None. `support`/`refute` are the claim's
    canonical counts. recency_id (ce.id / rowid, optional) breaks ties toward
    the newest finding; older callers may pass 3-tuples."""
    cands = []
    for row in rows:
        et, cf, kf = row[0], row[1], row[2]
        rec = row[3] if len(row) > 3 else 0
        c = _clean(kf)
        if not c:
            continue
        try:
            cf = float(cf) if cf is not None else 0.0
        except (TypeError, ValueError):
            cf = 0.0          # confidence is TEXT-affinity; some rows hold non-numeric strings
        try:
            rec = float(rec) if rec is not None else 0.0
        except (TypeError, ValueError):
            rec = 0.0
        cands.append((et, cf, c, rec))
    if not cands:
        return None
    dominant = 'support' if support >= refute else 'refute'
    # rank: provenance (arbitration>attack>ordinary) first, then dominant-verdict-
    # type, then real-conclusion. WITHIN the attack/arbitration tiers the LATEST
    # adjudication is the claim's current epistemic state and headlines (a fresh
    # NARROWED that found a specific residue is an artifact supersedes an older
    # SURVIVED of the core; a newer arbitration supersedes an older one). For
    # ordinary (tier-0) findings recency stays a weak FINAL tiebreak so the
    # tier-0 shelf does not churn — strong_rec is 0 for all of them, a no-op.
    def key(x):
        et, cf, kf, rec = x
        tier = _provenance_tier(kf)
        strong_rec = rec if tier >= 1 else 0
        return (tier, et == dominant, _has_verdict(kf), strong_rec,
                round(cf, 3), len(kf), rec)
    cands.sort(key=key, reverse=True)
    return cands[0][2]


def iter_claim_findings(conn, only_empty=False):
    """Yield (claim_id, support, refute, current_summary, [(et,cf,kf),...]) per
    non-meta claim that has at least one evidence row."""
    where = "AND (kc.claim_summary IS NULL OR kc.claim_summary='')" if only_empty else ""
    cur = conn.execute(f"""
        SELECT ce.claim_id, kc.support_count, kc.refute_count, kc.claim_summary,
               ce.evidence_type, ce.confidence, ce.key_finding, ce.id
        FROM claim_evidence ce
        JOIN knowledge_claims kc ON kc.id = ce.claim_id
        WHERE COALESCE(kc.is_meta,0)=0 {where}
        ORDER BY ce.claim_id
    """)
    cur_id, support, refute, summ, rows = None, 0, 0, None, []
    for cid, s, r, cs, et, cf, kf, eid in cur:
        if cid != cur_id:
            if cur_id is not None:
                yield cur_id, support, refute, summ, rows
            cur_id, support, refute, summ, rows = cid, s or 0, r or 0, cs, []
        rows.append((et, cf, kf, eid))
    if cur_id is not None:
        yield cur_id, support, refute, summ, rows


def sample(conn, status, limit=40):
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM knowledge_claims WHERE claim_status=? AND COALESCE(is_meta,0)=0 ORDER BY domain LIMIT ?",
        (status, limit))]
    idset = set(ids)
    picked = {}
    for cid, s, r, summ, rows in iter_claim_findings(conn):
        if cid in idset:
            picked[cid] = pick_summary(rows, s, r)
    for cid in ids:
        row = conn.execute("SELECT domain, hypothesis_text FROM knowledge_claims WHERE id=?", (cid,)).fetchone()
        new = picked.get(cid)
        print(f"\n[{row[0]}] claim #{cid}")
        print("  BEFORE: " + textwrap.shorten((row[1] or '').strip(), 150))
        print("  AFTER:  " + (textwrap.shorten(new, 200) if new else "‹no usable finding — keeps question as fallback›"))


def run(dry_run=True, only_empty=False):
    conn = sqlite3.connect(f'file:{DB}?mode=ro' if dry_run else DB,
                           uri=dry_run, timeout=30)
    if not dry_run:
        conn.execute('PRAGMA busy_timeout=30000')
    scanned = filled = skipped_nofind = 0
    batch, BATCH = [], 500
    t0 = time.time()
    for cid, s, r, summ, rows in iter_claim_findings(conn, only_empty=only_empty):
        scanned += 1
        new = pick_summary(rows, s, r)
        if not new:
            skipped_nofind += 1
            continue
        if new == (summ or None):
            continue
        filled += 1
        if not dry_run:
            batch.append((new, cid))
            if len(batch) >= BATCH:
                conn.executemany("UPDATE knowledge_claims SET claim_summary=? WHERE id=?", batch)
                conn.commit()          # short txn per batch — never hold the WAL write lock
                batch.clear()
    if batch and not dry_run:
        conn.executemany("UPDATE knowledge_claims SET claim_summary=? WHERE id=?", batch)
        conn.commit()
    conn.close()
    verb = "would fill" if dry_run else "filled"
    print(f"scanned {scanned} claims · {verb} {filled} summaries · "
          f"{skipped_nofind} had no usable finding · {time.time()-t0:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--only-empty', action='store_true',
                    help="only fill claims whose summary is currently empty")
    ap.add_argument('--sample', metavar='STATUS',
                    help="print before/after for N claims of this tier")
    args = ap.parse_args()

    if args.sample:
        conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
        sample(conn, args.sample)
    elif args.apply:
        run(dry_run=False, only_empty=args.only_empty)
    else:
        run(dry_run=True, only_empty=args.only_empty)


if __name__ == '__main__':
    main()
