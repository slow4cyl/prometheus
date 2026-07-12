#!/usr/bin/env python3
"""discovery_spotlight.py — find, harden, and surface the system's actual discoveries.

The whole pipeline exists to find mechanisms with NO published counterpart. Those
claims exist — `prior_work_status='LIT_NOT_FOUND'` on a REPLICATED/ESTABLISHED claim
is a result that survived replication (and, at ESTABLISHED, an adversarial attack)
AND that a literature audit could not find in prior work. But nothing distinguishes
them from the textbook rediscoveries that fill the shelf, and nothing gives them the
EXTRA scrutiny they specifically need — because a NOT_FOUND claim is the highest-stakes
item on the shelf: it is a genuine discovery XOR an error/artifact, with no textbook to
catch the difference.

This lane closes that gap:

  * score every NOT_FOUND survivor into a `discovery_candidates` ledger by a composite
    that rewards validation (tier, break-survivals, weighted support) AND literature-
    absence CONFIDENCE (a low-confidence NOT_FOUND is probably a search-miss, not a find);
  * for the credible ones, enqueue a DISCOVERY-HARDENING re-derivation attack via
    adversarial_replication_enqueuer.py --claim — an independent-method re-derivation
    that tries hardest to break the claim. For a REPLICATED candidate this doubles as
    the ESTABLISHED break-survival gate; for an ESTABLISHED one it is a second, harder
    attack. SURVIVED ⇒ a hardened genuine-novelty candidate; BROKEN ⇒ it was an artifact;
  * flag the low-confidence ones as likely-search-miss (do NOT spend an experiment
    hardening a claim the literature probably does cover — that is a re-audit job);
  * `--report` emits a ranked leaderboard and writes ~/.hermes/discovery_candidates.json
    for the discoveries artifact.

Same non-destructive machinery as the meta-prober / contradiction-attacker: own ledger
(survives the orphan sweep), --claim bypass + HYPOTHESIS re-embed hash-collides the
result back onto the same claim, trichotomy routes normally. hypothesis_text untouched;
posterior/tier never written here. Timestamps epoch float.

Usage:
    python3 discovery_spotlight.py --report            # leaderboard + json, read-only
    python3 discovery_spotlight.py --dry-run
    python3 discovery_spotlight.py --apply --limit 8   # score ledger + enqueue hardening
"""
import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import math
import os
import re
import subprocess
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discovery_routing as routing

DB = _PP_PROMETHEUS_DB
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ENQUEUER = os.path.join(SCRIPTS, 'adversarial_replication_enqueuer.py')
JSON_PATH = os.path.expanduser('~/.hermes/discovery_candidates.json')

# below this literature-absence confidence a NOT_FOUND is more likely a search-miss
# than a discovery — flag it, do not spend a hardening experiment on it
RE_AUDIT_CONF_FLOOR = 0.55
MAX_HARDEN = 8


def _novelty_ceiling():
    """Measured trust in the shelf's own novelty assertions, from the latest
    novelty_calibration --adjudicate run (recommended_ceiling = 1 - measured
    false-novelty rate). Env HERMES_NOVELTY_CEILING overrides; 1.0 (no-op)
    until an adjudication with a non-trivial sample exists. Multiplies the
    novelty-credit term in score() — the shelf's novelty prior is worth
    exactly what independent adjudication says it is."""
    env = os.environ.get('HERMES_NOVELTY_CEILING')
    if env:
        try:
            return max(0.0, min(1.0, float(env)))
        except ValueError:
            pass
    try:
        report = json.load(open(os.path.expanduser('~/.hermes/novelty_calibration.json')))
        for run in reversed(report.get('runs', [])):
            if (run.get('mode') == 'adjudicate' and run.get('recommended_ceiling') is not None
                    and (run.get('n_judged') or 0) >= 8):
                return max(0.0, min(1.0, float(run['recommended_ceiling'])))
    except Exception:
        pass
    return 1.0


NOVELTY_CEILING = _novelty_ceiling()


def ensure_ledger(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovery_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL UNIQUE,
            discovery_score REAL NOT NULL,
            novelty_confidence REAL,
            tier TEXT,
            wsc REAL,
            survivals INTEGER,
            hardening_task_id TEXT,
            status TEXT NOT NULL DEFAULT 'scored',  -- scored|hardening|likely_search_miss|hardened|broken
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_disc_score "
                 "ON discovery_candidates(discovery_score)")
    for col, decl in (("route", "TEXT"), ("route_reason", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE discovery_candidates ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass    # column already exists
    conn.commit()


def get_candidates(conn):
    """NOT_FOUND survivors with the full dossier the router reads: newest audit's
    confidence / residue / explanation / citations, the recorded prior-work citation,
    the empirical-fact flag, and the break-survival count. REPLICATED/ESTABLISHED,
    non-meta, non-empirical-fact (the exclusion footnote 3 promised is now enforced
    HERE, not only in the summary counts). Adversarial prose + mapped scope are pulled
    per-claim in the loop (they are 1-to-many)."""
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT kc.id, kc.claim_status AS tier, kc.claim_summary, kc.hypothesis_text,
               kc.domain, COALESCE(kc.weighted_support_count,0) AS wsc,
               kc.prior_work_citation, COALESCE(kc.is_empirical_fact,0) AS is_fact,
               na.confidence AS novelty_conf, na.novel_residue,
               na.explanation, na.citations, na.corroborated,
               COALESCE((SELECT COUNT(*) FROM adversarial_replications ar
                         WHERE ar.claim_id=kc.id AND ar.status='survived'),0) AS survivals
        FROM knowledge_claims kc
        JOIN novelty_audits na ON na.id=(SELECT MAX(na2.id) FROM novelty_audits na2
                                          WHERE na2.claim_id=kc.id)
        WHERE kc.prior_work_status='LIT_NOT_FOUND'
          AND kc.claim_status IN ('REPLICATED','ESTABLISHED')
          AND COALESCE(kc.is_meta,0)=0
          AND COALESCE(kc.is_empirical_fact,0)=0
          AND na.verdict='NOT_FOUND'
    """).fetchall()


def claim_dossier_extra(conn, cid):
    """The 1-to-many text the router also reads: latest mapped scope and the recent
    adversarial-replication result prose (where a worker's own 'PMID 40555181'
    confession lives)."""
    scope = conn.execute("SELECT scope_text FROM claim_scopes WHERE claim_id=? "
                         "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    adv = [a[0] for a in conn.execute(
        "SELECT (SELECT result FROM experiments e WHERE e.id=ar.experiment_id) "
        "FROM adversarial_replications ar WHERE ar.claim_id=? ORDER BY ar.id DESC LIMIT 4",
        (cid,)).fetchall() if a[0]]
    return (scope[0] if scope else ""), adv


def _independence_mult():
    """Per-claim independence multiplier from independence_gate's claim_independence
    ledger (bounded support-depth haircut for claims whose STAMPED support was
    prior-fed). Empty/1.0 until the gate's self-arming check arms it — so this is
    neutral by construction until the prior_fed stamp is proven and populated."""
    try:
        conn = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10)
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='claim_independence'").fetchone():
            return {}
        m = {r[0]: r[1] for r in conn.execute(
            "SELECT claim_id, COALESCE(independence_multiplier,1.0) FROM claim_independence")}
        conn.close()
        return m
    except Exception:
        return {}


# ── Decisiveness ────────────────────────────────────────────────────────────
# A REGIME_SPLIT ("it depends") resolves a question less than a decisive A_CORRECT/
# B_CORRECT/SURVIVED — yet regime-splits measure ~2x over-represented at the top of
# the shelf (they absorb attacks by narrowing where a crisp verdict would break, so
# the score rewards falsification-resistance-by-shape). A bounded, default-neutral
# discount ranks a decisive resolution above an "it depends" at equal robustness.
# The reviewer's harder sub-problem — telling a regime-split that maps a REAL boundary
# (#67218, "the two sides measure DIFFERENT QUANTITIES": smooth vs non-smooth
# classifiers) from one that DISSOLVES the question into a modeling choice (#65514,
# "the sign depends on the data-generating model") — gets a first cut here: the
# dissolving shape takes the heavier discount, a plain boundary-map the lighter one,
# and everything decisive stays 1.0. Refining the dissolving detector is a watch-item.
REGIME_SPLIT_FACTOR = 0.92   # maps a boundary — "it depends" on an external condition
DISSOLVING_FACTOR = 0.85     # dissolved into a modeling choice / generative assumption
_REGIME_SPLIT = re.compile(r"\bREGIME[_ ]SPLIT\b", re.I)
_DISSOLVING = re.compile(
    r"depends?\s+on\s+(?:the\s+|your\s+|one'?s\s+|its\s+)?"
    r"(?:data[- ]generating\s+(?:model|process)|generative\s+(?:model|assumption|process)|"
    r"modell?ing\s+(?:choice|assumption|decision)|(?:your|the)\s+assumption|prior\s+choice|"
    r"how\s+(?:you|one)\s+(?:defines?|parametriz\w+|models?|chooses?))", re.I)


def decisiveness_factor(summary):
    """Bounded multiplier (<=1.0) for how decisively the claim resolves its question.
    Default-neutral: a claim with no REGIME_SPLIT verdict prose returns 1.0 (no-op)."""
    s = summary or ""
    if not _REGIME_SPLIT.search(s):
        return 1.0
    return DISSOLVING_FACTOR if _DISSOLVING.search(s) else REGIME_SPLIT_FACTOR


def score(tier, novelty_conf, wsc, survivals, *, novelty_credit=None, indep_mult=1.0,
          decisiveness=1.0):
    """0..100, ranked by ROBUSTNESS: 45 validation-tier + 25 break-survivals + 15
    support-depth + 15 novelty-as-a-weak-prior. The old score gave literature-absence
    40 of 100 points and had no term at all for triviality — which is how a
    media-saturated paper and a textbook identity ranked #1 and #2. Novelty is now a
    GATE (the router) far more than a score term, and the term that remains is a
    DISCOUNTED prior on a single un-corroborated search. Callers pass
    novelty_credit=0.0 for off-shelf or model-internal claims (no novelty credit);
    None lets the default discount apply. Interpretable — no learned weights."""
    tier_w = 1.0 if tier == 'ESTABLISHED' else 0.6
    surv = min(int(survivals or 0), 4) / 4.0
    depth = min(math.log1p(float(wsc or 0)) / math.log(21), 1.0)   # ~1.0 at wsc>=20
    depth *= max(0.0, min(1.0, float(indep_mult)))   # independence-adjusted support depth
    if novelty_credit is None:
        conf = 0.5 if novelty_conf is None else max(0.0, min(1.0, float(novelty_conf)))
        novelty_credit = 0.4 * conf     # one un-corroborated web pass is weak evidence
    novelty_credit = max(0.0, min(1.0, float(novelty_credit)))
    # measured shelf-level trust from independent adjudication (1.0 until measured)
    novelty_credit *= NOVELTY_CEILING
    base = 45 * tier_w + 25 * surv + 15 * depth + 15 * novelty_credit
    # decisiveness ranks a crisp resolution above an "it depends" at equal robustness
    return round(base * max(0.80, min(1.0, float(decisiveness))), 1)


def build_note(claim_id, tier):
    gate = ("This is a REPLICATED claim, so surviving this independent re-derivation "
            "also earns the break-survival the ESTABLISHED gate requires. "
            if tier == 'REPLICATED' else
            "This claim is already ESTABLISHED; this is a SECOND, harder attack. ")
    return (
        f"DISCOVERY HARDENING. Claim #{claim_id} survived replication in this system AND "
        f"a literature audit found NO published counterpart for it. That makes it exactly "
        f"one of two things: a genuine discovery, or an error/artifact — and because it is "
        f"absent from the literature, there is no textbook to catch which. {gate}\n"
        f"Do NOT re-run the original method. RE-DERIVE the core result from scratch by an "
        f"INDEPENDENT method (different derivation, different estimator, or a from-first-"
        f"principles simulation) and try your hardest to break it.\n"
        f"Report ATTACK_OUTCOME: BROKEN if your independent derivation contradicts the "
        f"claim (it was an artifact); NARROWED if it holds only under conditions you should "
        f"state; SURVIVED if the independent re-derivation confirms it — which hardens it as "
        f"a genuine-novelty candidate."
    )


def upsert(conn, cid, sc, conf, tier, wsc, surv, status, task=None, route=None, reason=None):
    now = time.time()
    row = conn.execute("SELECT id, hardening_task_id FROM discovery_candidates "
                       "WHERE claim_id=?", (cid,)).fetchone()
    if row:
        conn.execute(
            "UPDATE discovery_candidates SET discovery_score=?, novelty_confidence=?, "
            "tier=?, wsc=?, survivals=?, status=?, hardening_task_id=COALESCE(?,hardening_task_id), "
            "route=?, route_reason=?, updated_at=? WHERE claim_id=?",
            (sc, conf, tier, wsc, surv, status, task, route, reason, now, cid))
    else:
        conn.execute(
            "INSERT INTO discovery_candidates (claim_id, discovery_score, novelty_confidence, "
            "tier, wsc, survivals, hardening_task_id, status, route, route_reason, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, sc, conf, tier, wsc, surv, task, status, route, reason, now, now))
    conn.commit()


def already_hardened(conn, cid):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='discovery_candidates'").fetchone():
        return False
    r = conn.execute("SELECT status FROM discovery_candidates WHERE claim_id=?",
                     (cid,)).fetchone()
    return bool(r) and r[0] in ('hardening', 'hardened', 'broken')


def write_report_json(conn, scored):
    """Build the JSON from `scored` — the SAME fresh routing pass this run computed
    over the current NOT_FOUND survivors — not from the ledger's stored route. The
    ledger route is a cache that lags the live DB (a claim can enter/leave the
    candidate set between cron phases); routing `scored` fresh makes the JSON shelf a
    faithful projection of the same routing the page renders, so n_shelf matches.
    All ledger rows are still dumped under `candidates` for archival."""
    shelf, off_shelf = [], []
    for x in scored:
        rec = {"claim_id": x["cid"], "score": x["score"], "tier": x["tier"],
               "novelty_confidence": x["conf"], "survivals": x["surv"], "route": x["route"],
               "route_reason": x["reason"], "domain": x["domain"],
               "sim_flagged": bool(x.get("sim")), "summary": (x["summary"] or "")[:400]}
        (shelf if x["route"] == routing.DISCOVERY else off_shelf).append(rec)
    have_route = any(c[1] == "route" for c in
                     conn.execute("PRAGMA table_info(discovery_candidates)"))
    rcol = "dc.route, dc.route_reason" if have_route else "NULL, NULL"
    ledger = [{"claim_id": r[0], "score": r[1], "novelty_confidence": r[2], "tier": r[3],
               "status": r[6], "route": r[7], "route_reason": r[8], "domain": r[9],
               "summary": (r[10] or "")[:400]}
              for r in conn.execute(f"""
        SELECT dc.claim_id, dc.discovery_score, dc.novelty_confidence, dc.tier,
               dc.wsc, dc.survivals, dc.status, {rcol}, kc.domain, kc.claim_summary
        FROM discovery_candidates dc JOIN knowledge_claims kc ON kc.id=dc.claim_id
        ORDER BY dc.discovery_score DESC""").fetchall()]
    snap = {"generated_at": time.time(), "n": len(ledger), "n_shelf": len(shelf),
            "n_off_shelf": len(off_shelf), "shelf": shelf, "off_shelf": off_shelf,
            "candidates": ledger}
    with open(JSON_PATH + ".tmp", "w") as f:
        json.dump(snap, f, indent=1)
    os.replace(JSON_PATH + ".tmp", JSON_PATH)
    return snap


def main():
    ap = argparse.ArgumentParser(description="Spotlight and harden genuine-novelty claims")
    ap.add_argument('--apply', action='store_true', help='score ledger + enqueue hardening')
    ap.add_argument('--report', action='store_true', help='leaderboard + json (read-only)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=MAX_HARDEN)
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(f'file:{DB}{"" if apply else "?mode=ro"}', uri=True, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    if apply:
        ensure_ledger(conn)

    cands = get_candidates(conn)
    indep = _independence_mult()   # {} until the independence gate arms; then bounded haircuts
    unrec = routing.unreconciled_claims(conn)   # reconciliation gate ({} until critic runs)
    scored = []
    for r in cands:
        scope_txt, adv = claim_dossier_extra(conn, r["id"])
        route, reason = routing.route_claim(
            summary=r["claim_summary"] or "", hypothesis=r["hypothesis_text"] or "",
            residue=r["novel_residue"] or "", explanation=r["explanation"] or "",
            citations=r["citations"] or "", scope=scope_txt, adversarial_texts=adv,
            prior_work_citation=r["prior_work_citation"] or "",
            is_empirical_fact=bool(r["is_fact"]), novelty_confidence=r["novelty_conf"],
            conf_floor=RE_AUDIT_CONF_FLOOR)
        if r["id"] in unrec:
            # reconciliation gate: headline and mapped scope assert different
            # propositions — no canonical claim to shelve until arbitration
            # reconciles them (the critic enqueues that task itself)
            route, reason = routing.UNRECONCILED, unrec[r["id"]][:200]
        sim = routing.simulation_flag(r["novel_residue"] or "", r["claim_summary"] or "", scope_txt)
        # novelty credit: none for off-shelf or model-internal claims; the full
        # weak prior when a second family independently corroborated the absence
        # (novelty_audit finder, corroborated=1); else the single-search 0.4
        # discount applies inside score(). All paths ride under NOVELTY_CEILING.
        if route != routing.DISCOVERY or sim:
            nov_credit = 0.0
        elif r["corroborated"] == 1:
            nov_credit = 0.5 if r["novelty_conf"] is None else max(
                0.0, min(1.0, float(r["novelty_conf"])))
        else:
            nov_credit = None
        sc = score(r["tier"], r["novelty_conf"], r["wsc"], r["survivals"],
                   novelty_credit=nov_credit, indep_mult=indep.get(r["id"], 1.0),
                   decisiveness=decisiveness_factor(r["claim_summary"] or ""))
        scored.append({"cid": r["id"], "tier": r["tier"],
                       "summary": r["claim_summary"] or r["hypothesis_text"], "domain": r["domain"],
                       "wsc": r["wsc"], "conf": r["novelty_conf"], "surv": r["survivals"],
                       "score": sc, "route": route, "reason": reason, "sim": bool(sim)})
    scored.sort(key=lambda x: -x["score"])

    # report-only path (also runs at the end of --apply once the ledger is fresh)
    if args.report and not apply:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='discovery_candidates'").fetchone():
            print("No discovery_candidates ledger yet — run --apply first.")
            conn.close()
            return 0
        snap = write_report_json(conn, scored)
        shelf = snap["shelf"]
        print(f"discovery leaderboard ({snap['n_shelf']} on shelf / {snap['n']} scored)  → {JSON_PATH}\n")
        print(f"  {'claim':>7s} {'score':>6s} {'tier':>11s} {'conf':>5s} {'surv':>4s}  domain / summary")
        for r in shelf[:25]:
            print(f"  #{r['claim_id']:<6d} {r['score']:>6.1f} {r['tier']:>11s} "
                  f"{(r['novelty_confidence'] or 0):>5.2f} {r['survivals']:>4d}  "
                  f"[{r['domain']}] {(r['summary'] or '')[:80]}")
        conn.close()
        return 0

    # dry-run: show what would be scored, routed, and hardened
    if not apply:
        n_shelf = sum(1 for x in scored if x["route"] == routing.DISCOVERY)
        print(f"{len(scored)} NOT_FOUND survivors  ({n_shelf} on shelf, "
              f"{len(scored) - n_shelf} routed off)\n")
        for x in scored[:args.limit]:
            if x["route"] != routing.DISCOVERY:
                action = f"OFF-SHELF [{x['route']}] — {x['reason'][:60]}"
            else:
                action = "would HARDEN" + ("  (sim-flagged: novelty=0)" if x["sim"] else "")
            print(f"  #{x['cid']:<6d} score={x['score']:>5.1f} [{x['tier']}] "
                  f"conf={(x['conf'] or 0):.2f} surv={x['surv']} -> {action}\n"
                  f"      [{x['domain']}] {(x['summary'] or '')[:90]}")
        conn.close()
        return 0

    # apply: ledger everything; harden ONLY genuine discovery-route candidates.
    # off-shelf routes (known-in-lit / empirical / derivable / search-miss) are
    # binned with their reason and never spend a hardening experiment — no point
    # re-deriving a textbook identity or a paper we already cite.
    hardened, binned = 0, 0
    for x in scored:
        cid = x["cid"]
        conf = x["conf"] if x["conf"] is not None else 0.5
        route, reason = x["route"], x["reason"]
        if route != routing.DISCOVERY:
            st = 'likely_search_miss' if route == routing.SEARCH_MISS else 'off_shelf'
            upsert(conn, cid, x["score"], conf, x["tier"], x["wsc"], x["surv"], st,
                   route=route, reason=reason)
            binned += 1
            continue
        if already_hardened(conn, cid):
            upsert(conn, cid, x["score"], conf, x["tier"], x["wsc"], x["surv"],
                   status=_current_status(conn, cid), route=route, reason=reason)
            continue
        if hardened >= args.limit:
            upsert(conn, cid, x["score"], conf, x["tier"], x["wsc"], x["surv"], 'scored',
                   route=route, reason=reason)
            continue
        note = build_note(cid, x["tier"])
        res = subprocess.run([sys.executable, ENQUEUER, '--claim', str(cid), '--note', note],
                             capture_output=True, text=True, timeout=180)
        task = re.search(r't_[a-f0-9]+', res.stdout or '')
        live = 'already has a live attack' in (res.stdout or '')
        ok = res.returncode == 0 and not live
        upsert(conn, cid, x["score"], conf, x["tier"], x["wsc"], x["surv"],
               'hardening' if ok else 'scored', task.group(0) if task else None,
               route=route, reason=reason)
        if ok:
            hardened += 1
            print(f"HARDENING #{cid} [{x['tier']}] score={x['score']} "
                  f"({task.group(0) if task else 'no-task'})")
        elif live:
            print(f"  #{cid}: already has a live attack — ledgered, not re-enqueued")

    snap = write_report_json(conn, scored)
    conn.close()
    print(f"\nledgered {len(scored)} candidates ({snap['n_shelf']} on shelf, {binned} routed off), "
          f"enqueued {hardened} hardening attack(s) → {JSON_PATH}")
    return 0


def _current_status(conn, cid):
    r = conn.execute("SELECT status FROM discovery_candidates WHERE claim_id=?",
                     (cid,)).fetchone()
    return r[0] if r else 'scored'


if __name__ == '__main__':
    main()
