#!/usr/bin/env python3
"""meta_claim_prober.py — make mature meta-claims earn their tier by prediction.

Meta-claims (knowledge_claims.is_meta=1) are the system's beliefs about its own
transferable mechanisms ("physical dynamics transfers to fluid dynamics (79%,
19 transfers)"). Today they are EXEMPT from every falsification lane: both the
adversarial enqueuer and the dispute arbitrator filter is_meta=0, so a meta-claim
climbs on PASSIVE hash-collision evidence alone and is never actively tested
(adversarial_replications on is_meta=1: zero). A generalization a system asserts
about itself but never risks is exactly the kind of belief that should be risked.

This lane closes that gap using the machinery that already exists — no edit to
the intake router or the enqueuer core:

  * pick a mature, testable meta-claim (well-supported, transfer/mechanism-shaped,
    NOT a named-internal-component claim — those cannot be domain-transfer-tested);
  * pick a target domain it was NOT built from (unseen in its evidence);
  * enqueue via adversarial_replication_enqueuer.py --claim (whose --claim path
    deliberately bypasses the is_meta / REPLICATED filters), with a note that
    turns the attack into a GENERALIZATION test and PREREGISTERS a signed
    direction before the run.

Attach-back is automatic and needs no new path: the attack card re-embeds the
meta-claim's HYPOTHESIS line, so the worker result hash-collides onto the SAME
meta-claim through attach_experiment — and ATTACK_OUTCOME routes through the
normal trichotomy (BROKEN⇒the generalization fails / DISPUTED, NARROWED⇒domain-
limited, SURVIVED⇒it generalizes). The preregistered direction the note requests
flows into worker_results.predicted/observed_direction, so these probes also feed
the Lane-3 prior-override report for free.

Ledger: meta_transfer_predictions records (meta_claim, target_domain, prediction)
for dedup + observability; the attack itself is tracked in adversarial_replications.

Timestamps epoch float (time.time()); never ISO. hypothesis_text never touched.

Usage:
    python3 meta_claim_prober.py --dry-run
    python3 meta_claim_prober.py --apply --limit 3
"""
import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import random
import re
import subprocess
import sqlite3
import sys
import time

DB = _PP_PROMETHEUS_DB
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ENQUEUER = os.path.join(SCRIPTS, 'adversarial_replication_enqueuer.py')

MIN_SUPPORT = 6            # only well-supported meta-claims are worth risking
MAX_PROBE = 3             # cap per run — these spawn cross-domain experiments
MAX_TARGETS_PER_CLAIM = 3  # probe each meta-claim in up to K distinct unseen domains:
                           # n=1 is coin-flip noise, so "generalizes" only becomes a
                           # gradient (held in j/k domains) with several targets
# meta-claims naming internal machinery can't be domain-transfer-tested; skip them
_INTERNAL = re.compile(
    r'\b(task_refiller|apply_worker_results|kanban|worker_results|prometheus\.db|'
    r'curiosit|synthesis|cron|dispatcher|scorer|refiller|pipeline stage|the system)\b',
    re.I)
# a testable meta-claim asserts a transfer / general relationship
_TRANSFERABLE = re.compile(r'transfer|generaliz|across (domains|fields)|mechanism|principle', re.I)


def ensure_ledger(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta_transfer_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meta_claim_id INTEGER NOT NULL,
            target_domain TEXT NOT NULL,
            predicted_direction INTEGER,      -- +1 predicted to generalize, -1 not
            kanban_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mtp_claim "
                 "ON meta_transfer_predictions(meta_claim_id)")
    # reconciliation columns (added 2026-07-04): the probe fires the attack, then
    # a later --reconcile pass reads the resolved outcome + the worker's signed
    # pre/post direction back into the ledger. Migrate in place so old rows keep.
    have = {r[1] for r in conn.execute("PRAGMA table_info(meta_transfer_predictions)")}
    for col, decl in (("observed_direction", "INTEGER"),   # +1 held / 0 mixed / -1 failed
                      ("outcome", "TEXT"),                  # survived|narrowed|refuted
                      ("reconciled_at", "REAL")):
        if col not in have:
            conn.execute(f"ALTER TABLE meta_transfer_predictions ADD COLUMN {col} {decl}")
    conn.commit()


def _parse_direction(js):
    """A worker's direction field is JSON like {"generalizes_to_nlp": 1}. Return the
    single signed int (-1/0/+1), or None if unparseable. The key varies by target so
    we take the sole value rather than matching the key."""
    if not js:
        return None
    try:
        d = json.loads(js)
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or not d:
        return None
    # prefer a generalizes_to_* key; else the lone value
    vals = [v for k, v in d.items() if str(k).startswith('generalizes_to')] or list(d.values())
    try:
        v = int(round(float(vals[0])))
    except (ValueError, TypeError, IndexError):
        return None
    return max(-1, min(1, v))


def reconcile(conn):
    """Read resolved outcomes back into the ledger. For each probe still marked
    'enqueued'/'pending', find its adversarial_replication (by claim+task) and the
    worker_result behind it, then record: outcome (survived/narrowed/refuted),
    predicted_direction, observed_direction. Prints a calibration summary — the
    whole point of the lane is to score the system's beliefs ABOUT its own transfer,
    and predicted-vs-observed is exactly that signal.

    Read-only joins over live tables; the only writes are per-row ledger UPDATEs
    (short txn, commit at end). worker_results / adversarial_replications are never
    mutated."""
    rows = conn.execute("""
        SELECT m.id, m.meta_claim_id, m.target_domain, m.kanban_task_id,
               ar.status AS outcome, ar.experiment_id
        FROM meta_transfer_predictions m
        JOIN adversarial_replications ar
          ON ar.claim_id = m.meta_claim_id AND ar.kanban_task_id = m.kanban_task_id
        WHERE ar.resolved_at IS NOT NULL
          AND (m.reconciled_at IS NULL OR m.status IN ('enqueued','pending'))
    """).fetchall()
    n = 0
    tally = {}
    cal = {"scored": 0, "hit": 0, "overconfident": 0}
    for r in rows:
        mid, cid, target, task, outcome, exp = r
        wr = conn.execute(
            "SELECT predicted_direction, observed_direction FROM worker_results "
            "WHERE experiment_id=? ORDER BY CAST(created_at AS REAL) DESC LIMIT 1",
            (exp,)).fetchone()
        pred = _parse_direction(wr[0]) if wr else None
        obs = _parse_direction(wr[1]) if wr else None
        conn.execute(
            "UPDATE meta_transfer_predictions SET outcome=?, predicted_direction=?, "
            "observed_direction=?, status='reconciled', reconciled_at=? WHERE id=?",
            (outcome, pred, obs, time.time(), mid))
        n += 1
        tally[outcome] = tally.get(outcome, 0) + 1
        if pred is not None and obs is not None:
            cal["scored"] += 1
            # hit = the sign the system committed to matches what it observed
            if (pred > 0) == (obs > 0):
                cal["hit"] += 1
            # overconfident = predicted it WOULD generalize (+1) but it did not (<=0)
            if pred > 0 and obs <= 0:
                cal["overconfident"] += 1
    conn.commit()
    # Persist a full-ledger summary snapshot to a JSON sidecar so other crons
    # (score_curiosities.py) can read the measured generalization rates.
    # Best-effort: a write failure never breaks reconciliation.
    try:
        _all = conn.execute(
            "SELECT outcome, predicted_direction, observed_direction "
            "FROM meta_transfer_predictions WHERE status='reconciled'").fetchall()
        _tot = {"scored": 0, "hit": 0, "over": 0}
        _out = {}
        for _o, _p, _ob in _all:
            _out[_o] = _out.get(_o, 0) + 1
            if _p is not None and _ob is not None:
                _tot["scored"] += 1
                if (_p > 0) == (_ob > 0):
                    _tot["hit"] += 1
                if _p > 0 and _ob <= 0:
                    _tot["over"] += 1
        import json as _json
        _path = os.path.expanduser("~/.hermes/meta_transfer_calibration.json")
        _snap = {"n_reconciled": len(_all), "n_scored": _tot["scored"],
                 "hit_rate": round(_tot["hit"] / _tot["scored"], 4) if _tot["scored"] else None,
                 "overconfident": _tot["over"],
                 "overconfidence_rate": round(_tot["over"] / _tot["scored"], 4) if _tot["scored"] else None,
                 "outcomes": _out, "last_updated": time.time()}
        with open(_path + ".tmp", "w") as _f:
            _json.dump(_snap, _f, indent=1)
        os.replace(_path + ".tmp", _path)
        print(f"  wrote {_path}")
    except Exception as _we:
        print(f"  WARN: snapshot write failed: {_we}")
    print(f"reconciled {n} probe(s)")
    if tally:
        print("  outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    if cal["scored"]:
        hr = cal["hit"] / cal["scored"]
        print(f"  calibration: {cal['hit']}/{cal['scored']} signed predictions correct "
              f"({hr:.0%}); {cal['overconfident']} overconfident "
              f"(predicted generalize, observed did not)")
    return n


def probes_done(conn, claim_id):
    """How many distinct targets this meta-claim has already been probed in."""
    # ledger may not exist yet on a read-only dry-run (created under --apply)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='meta_transfer_predictions'").fetchone():
        return 0
    return conn.execute("SELECT COUNT(*) FROM meta_transfer_predictions "
                        "WHERE meta_claim_id=?", (claim_id,)).fetchone()[0]


def get_candidates(conn, limit):
    """Mature, testable meta-claims with capacity for another target. Breadth-first:
    every mature meta-claim gets probed once before any gets a 2nd/3rd target (a
    calibration built on many claims × 1 domain is more robust than a few claims ×
    3), then within the same probe-count the most-established self-belief is risked
    first."""
    rows = conn.execute("""
        SELECT id, hypothesis_text, claim_summary, domain, support_count,
               claim_status, COALESCE(weighted_support_count,0) AS wsc
        FROM knowledge_claims
        WHERE is_meta=1 AND support_count >= ?
          AND claim_status NOT IN ('RETIRED','DISPUTED')
    """, (MIN_SUPPORT,)).fetchall()
    scored = []
    for r in rows:
        text = (r[2] or r[1] or '')
        if _INTERNAL.search(text) or not _TRANSFERABLE.search(text):
            continue
        done = probes_done(conn, r[0])
        if done >= MAX_TARGETS_PER_CLAIM:
            continue
        scored.append((done, -float(r[6] or 0), r))
    scored.sort(key=lambda t: (t[0], t[1]))   # fewest-probed first, then highest wsc
    return [t[2] for t in scored[:limit]]


def seen_domains(conn, claim_id):
    return {d[0] for d in conn.execute(
        "SELECT DISTINCT domain FROM claim_evidence WHERE claim_id=? AND domain IS NOT NULL",
        (claim_id,)) if d[0]}


def populated_domains(conn):
    """Well-populated domains that make a meaningful transfer target."""
    return [d[0] for d in conn.execute("""
        SELECT domain FROM claim_evidence WHERE domain IS NOT NULL
        GROUP BY domain HAVING COUNT(*) >= 20 ORDER BY COUNT(*) DESC LIMIT 120""")]


def pick_target(conn, claim_id, pool, rng):
    seen = seen_domains(conn, claim_id)
    home = (conn.execute("SELECT domain FROM knowledge_claims WHERE id=?",
                         (claim_id,)).fetchone() or [None])[0]
    seen.add(home)
    # multi-target: also exclude domains this claim was ALREADY probed in, so each
    # of its up-to-K probes lands in a fresh unseen domain (no wasted re-test).
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='meta_transfer_predictions'").fetchone():
        for (d,) in conn.execute("SELECT target_domain FROM meta_transfer_predictions "
                                 "WHERE meta_claim_id=?", (claim_id,)):
            if d:
                seen.add(d)
    unseen = [d for d in pool if d not in seen]
    return rng.choice(unseen) if unseen else None


def build_note(claim_id, target, hypo):
    return (
        f"META-CLAIM GENERALIZATION TEST. This is claim #{claim_id}, a belief the "
        f"system holds about one of its own transferable mechanisms. It has only "
        f"ever been supported by evidence in the domains it was built from — it has "
        f"NEVER been risked in a domain it did not come from. Test whether the "
        f"mechanism GENERALIZES to an unseen domain: {target}.\n"
        f"PREREGISTER before running: in your written finding, first state a signed "
        f"prediction — does the mechanism hold in {target} (+1) or fail there (-1)? "
        f"— then design and run a real experiment in {target} that could refute it.\n"
        f"When you write the result, pass BOTH preregistration fields so the prior-"
        f"override report scores this: --predicted-direction '{{\"generalizes_to_{target}\": 1}}' "
        f"(use -1 if you predicted failure) and --observed-direction "
        f"'{{\"generalizes_to_{target}\": <+1 if it held, -1 if it failed, 0 if mixed>}}'.\n"
        f"Report ATTACK_OUTCOME: SURVIVED if the mechanism generalizes to {target}, "
        f"NARROWED if it holds only under restricted conditions there (state them), "
        f"BROKEN if it does not generalize."
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--reconcile', action='store_true',
                    help='read resolved probe outcomes + signed pre/post directions '
                         'back into the ledger and print a calibration summary')
    ap.add_argument('--limit', type=int, default=MAX_PROBE)
    ap.add_argument('--seed', type=int, default=None,
                    help='fix the target-domain RNG for reproducible runs')
    args = ap.parse_args()
    apply = args.apply and not args.dry_run
    need_write = apply or args.reconcile
    rng = random.Random(args.seed)

    conn = sqlite3.connect(f'file:{DB}{"" if need_write else "?mode=ro"}', uri=True, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    if need_write:
        ensure_ledger(conn)

    # reconcile first so an hourly --reconcile --apply run closes the loop on
    # already-resolved probes before spending on new ones.
    if args.reconcile:
        reconcile(conn)
        if not (args.apply or args.dry_run):
            conn.close()
            return 0

    cands = get_candidates(conn, args.limit)
    if not cands:
        print("No mature, testable, un-probed meta-claims.")
        conn.close()
        return 0

    pool = populated_domains(conn)
    done = 0
    for r in cands:
        cid = r[0]
        target = pick_target(conn, cid, pool, rng)
        if not target:
            print(f"  meta-claim #{cid}: no unseen target domain — skipping")
            continue
        label = (r[2] or r[1] or '')[:90]
        if not apply:
            print(f"[DRY-RUN] would probe meta-claim #{cid} [{r[5]} sup={r[4]}] "
                  f"-> generalize to '{target}'\n    {label}")
            done += 1
            continue
        note = build_note(cid, target, r[1])
        res = subprocess.run(
            [sys.executable, ENQUEUER, '--claim', str(cid), '--note', note],
            capture_output=True, text=True, timeout=180)
        task = re.search(r't_[a-f0-9]+', res.stdout or '')
        ok = res.returncode == 0 and 'already has a live attack' not in (res.stdout or '')
        conn.execute(
            "INSERT INTO meta_transfer_predictions (meta_claim_id, target_domain, "
            "predicted_direction, kanban_task_id, status, created_at) VALUES (?,?,?,?,?,?)",
            (cid, target, None, task.group(0) if task else None,
             'enqueued' if ok else 'enqueue_failed', time.time()))
        conn.commit()
        done += 1
        print(f"{'PROBED' if ok else 'FAILED'} meta-claim #{cid} -> generalize to "
              f"'{target}' ({task.group(0) if task else 'no-task'})")
        if not ok:
            print(f"    enqueuer: {(res.stdout or '')[-160:]} {(res.stderr or '')[-120:]}")

    conn.close()
    print(f"\n{'would probe' if not apply else 'probed'} {done} meta-claim(s)")
    return 0


if __name__ == '__main__':
    main()
