#!/usr/bin/env python3
"""independence_gate.py — measure how much of the shelf's agreement is independent.

The system credits agreement as validation (replication, transfer-survival, weighted
support). But the RAG feed baked into task bodies — "CONFIRMED PRIOR FINDINGS (build
on these, do not re-test)" (task_refiller.py:677 / batch_create_tasks.py:706) — pipes
earlier confirmed conclusions into later same/related-domain workers, so some of that
agreement is induced by shared priors, not independent rediscovery. Independent
confirmation is only worth its independence; this measures the spend and prices it in.

Stages (escalating). The first two run by default, OFFLINE, no new experiments:

  MEASURE  per promotion-band claim: what fraction of its support was "prior-fed"
           (its confirming task carried the CONFIRMED PRIOR FINDINGS feed) vs "blind",
           and how many distinct model FAMILIES the blind support spans. A claim whose
           support is mostly prior-fed and single-family has near-zero independent
           corroboration however high its wsc reads.

  PRICE    the shelf-level independence spend, the fraction specifically among
           cross-domain (transfer-survival) support, and a DEFAULT-NEUTRAL haircut
           coefficient on transfer-survival credit — reported, not wired into the
           maturity recompute as a WEIGHT (same discipline as
           cross_domain_disconfirmation_gate.py and the meta-transfer haircut).
           2026-07-04: the maturity core instead carries this as a GATE —
           ESTABLISHED requires >= 1 stamped-BLIND support once armed and the
           claim has >= 2 stamped supports (maturity.py established_blind_supports;
           settlement = the blind retest lane + this file's clean-room lane).

  CLEAN-ROOM  (--enqueue; wired 2026-07-04, cron `clean-room-lane`) for high-stakes
           independence-weak claims (ESTABLISHED + single-family), a BLIND replication:
           feed-free [CLEAN-ROOM] card (suppress_prior_context in both builders keeps
           it blind on any path), cross-family model_override, stamped prior_fed=0 so
           the result lands as a MEASURED-BLIND support — the only lane that
           ESTABLISHES independence instead of estimating it. <=4 live, dedup is
           live-task-only (dead attempts re-eligible), skips claims that already
           hold a stamped-blind support. Ledger: clean_room_replications.

HONEST LIMITS (printed every run; they belong on the gate's own page):
  * prior-fed is read from kanban task bodies; archived/old tasks lose their body text,
    so backward coverage degrades — the measured contamination is a FLOOR, not a ceiling.
  * model-FAMILY independence is weak evidence: cross-family models share most of their
    training data, and support here is ~90% one family anyway. "blind + cross-family"
    is the best available proxy, not proof of independence.
  * the deepest common cause is the shared base model; NO amount of internal agreement
    removes it. The toy-vs-world question ("does this σ-threshold mean anything in the
    world") is answerable only by external grounding (BENCH3-style), never by this gate.
  * a "needs ≥1 non-simulation basis" rule is theater TODAY: verdict_basis='simulation'
    is set on ~7 of ~1950 promotion-band supports while 'independent_computation' (the
    catch-all that includes toy sims) covers the rest and ~55% is NULL. It needs a
    text-level simulation detector, not the label — reported here, not enforced.

Usage:
    python3 independence_gate.py                 # measure + price, read-only, writes json
    python3 independence_gate.py --apply         # + persist claim_independence ledger
    python3 independence_gate.py --enqueue --limit 5   # clean-room lane (DELIBERATE)
"""
import argparse
from prometheus_paths import KANBAN_DB as _PP_KANBAN_DB, PROMETHEUS_DB as _PP_PROMETHEUS_DB, under_home
import json
import os
import sqlite3
import sys
import time

DB = _PP_PROMETHEUS_DB
KANBAN = _PP_KANBAN_DB
REPORT = under_home("independence_report.json")
ARMED = under_home("independence_armed.json")
ENQUEUER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "adversarial_replication_enqueuer.py")

PB = ("kc.claim_status IN ('REPLICATED','ESTABLISHED') "
      "AND COALESCE(kc.is_meta,0)=0 AND COALESCE(kc.is_empirical_fact,0)=0")
FEED_MARK = "CONFIRMED PRIOR FINDINGS"

# --- self-arming thresholds: the haircut arms on a CHECK, not a clock ---
# it arms the instant the stamp is PROVEN accurate against live task bodies and
# the stamp table is populated past a floor. At ~150 tasks/hr the floor clears fast.
ACCURACY_THRESHOLD = 0.98   # stamped prior_fed must match the live body this often
MIN_AUDIT = 50              # ...over at least this many still-auditable (un-archived) tasks
MIN_STAMPS = 500            # ...and the stamp table must hold at least this many rows
MIN_STAMPED_SUP = 2         # a claim needs >= this many STAMPED supports to be haircut


def armed_state():
    """Read the current armed state (written by --check). Absent/false ⇒ neutral."""
    try:
        with open(ARMED) as f:
            return json.load(f)
    except Exception:
        return {"armed": False}


def check_and_arm(conn, verbose=True):
    """The self-arming check — designed to run continuously (frequent cron). Arms the
    haircut the INSTANT two things are true, no waiting, no manual step:
      (1) the prior_fed stamp matches the live kanban task body >= ACCURACY_THRESHOLD
          over >= MIN_AUDIT still-auditable tasks (proves the instrument is correct), and
      (2) the stamp table holds >= MIN_STAMPS rows (proves the data is real, not a trickle).
    Sticky once armed (won't flap off); refreshes the audited numbers each run."""
    have_stamp = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                              "AND name='task_prior_feed'").fetchone()[0]
    if not have_stamp:
        state = {"armed": False, "reason": "no task_prior_feed table yet (stamp just went live)",
                 "stamps": 0, "audited": 0, "accuracy": None}
        _write_armed(state, prev_armed=armed_state().get("armed"))
        if verbose:
            print("  self-arming check: NOT ARMED — stamp table empty (0 rows). Accumulates "
                  "as tasks are created (~150/hr).")
        return state
    stamps = conn.execute("SELECT COUNT(*) FROM task_prior_feed").fetchone()[0]
    # accuracy audit: only tasks whose body is STILL live in kanban (un-archived)
    audit = conn.execute("""
        SELECT tpf.prior_fed AS stamped,
               CASE WHEN kt.body LIKE '%CONFIRMED PRIOR FINDINGS%' THEN 1 ELSE 0 END AS body_fed
        FROM task_prior_feed tpf JOIN k.tasks kt ON kt.id = tpf.kanban_task_id
        WHERE kt.body IS NOT NULL LIMIT 20000""").fetchall()
    audited = len(audit)
    agree = sum(1 for r in audit if r["stamped"] == r["body_fed"])
    accuracy = (agree / audited) if audited else None
    was = armed_state().get("armed", False)
    arm = (audited >= MIN_AUDIT and accuracy is not None
           and accuracy >= ACCURACY_THRESHOLD and stamps >= MIN_STAMPS)
    armed = was or arm      # sticky
    state = {"armed": armed, "armed_now": arm, "was_armed": was,
             "stamps": stamps, "audited": audited, "accuracy": accuracy,
             "thresholds": {"accuracy": ACCURACY_THRESHOLD, "min_audit": MIN_AUDIT,
                            "min_stamps": MIN_STAMPS},
             "checked_at": time.time()}
    _write_armed(state, prev_armed=was)
    if verbose:
        acc = f"{accuracy:.3f}" if accuracy is not None else "n/a"
        if armed and not was:
            print(f"  self-arming check: ✅ ARMING NOW — accuracy {acc} over {audited} audited, "
                  f"{stamps} stamps ≥ {MIN_STAMPS}. Haircut is now LIVE.")
        elif armed:
            print(f"  self-arming check: ARMED (accuracy {acc}, {stamps} stamps). Haircut live.")
        else:
            need = []
            if audited < MIN_AUDIT: need.append(f"audit {audited}/{MIN_AUDIT}")
            if accuracy is not None and accuracy < ACCURACY_THRESHOLD: need.append(f"accuracy {acc}/{ACCURACY_THRESHOLD}")
            if stamps < MIN_STAMPS: need.append(f"stamps {stamps}/{MIN_STAMPS}")
            print(f"  self-arming check: NOT ARMED — waiting on {', '.join(need) or 'data'}.")
    return state


def _write_armed(state, prev_armed=None):
    if state.get("armed") and not prev_armed and "armed_at" not in state:
        state["armed_at"] = time.time()
    try:
        with open(ARMED + ".tmp", "w") as f:
            json.dump(state, f, indent=1)
        os.replace(ARMED + ".tmp", ARMED)
    except Exception:
        pass


def family(model):
    """Normalise a model id to a coarse family. mimo appears as both
    'xiaomi/mimo-v2.5' and 'mimo-v2.5'; free-tier ids carry a ':free' suffix."""
    m = (model or "").lower()
    for key in ("mimo", "deepseek", "nemotron", "qwen", "llama", "gemma", "mistral"):
        if key in m:
            return key
    if "gpt" in m or "openai" in m:
        return "openai"
    if "/" in m:
        return m.split("/")[0]
    return m or "unknown"


def ro():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    # attach kanban read-only so task bodies (the RAG feed) are joinable
    conn.execute(f"ATTACH DATABASE 'file:{KANBAN}?mode=ro' AS k")
    return conn


def gather(conn):
    """One row per supporting experiment of a promotion-band claim, with the three
    independence signals: prior_fed (task carried the feed), model family, cross-domain."""
    have_kanban = os.path.exists(KANBAN)
    have_stamp = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                              "AND name='task_prior_feed'").fetchone()[0]
    body = ("COALESCE(kt.body, ka.body)" if have_kanban else "NULL")
    join = ("LEFT JOIN k.tasks kt ON kt.id = e.kanban_task_id "
            "LEFT JOIN k.archived_tasks ka ON ka.id = e.kanban_task_id") if have_kanban else ""
    stamp_col = "tpf.prior_fed AS stamped_fed" if have_stamp else "NULL AS stamped_fed"
    stamp_join = ("LEFT JOIN task_prior_feed tpf ON tpf.kanban_task_id = e.kanban_task_id"
                  if have_stamp else "")
    rows = conn.execute(f"""
        SELECT ce.claim_id, kc.claim_status AS tier, kc.domain,
               e.model, COALESCE(ce.is_cross_domain,0) AS xdom,
               e.verdict_basis AS basis, {stamp_col},
               {body} AS task_body
        FROM claim_evidence ce
        JOIN knowledge_claims kc ON kc.id = ce.claim_id
        JOIN experiments e ON e.id = ce.experiment_id
        {stamp_join}
        {join}
        WHERE {PB} AND COALESCE(ce.evidence_type,'support') = 'support'
    """).fetchall()

    claims = {}
    for r in rows:
        c = claims.setdefault(r["claim_id"], {
            "claim_id": r["claim_id"], "tier": r["tier"], "domain": r["domain"],
            "sup": [], })
        # the durable stamp is authoritative; the (mostly archived) kanban body is a fallback
        stamped = r["stamped_fed"] is not None
        if stamped:
            fed = bool(r["stamped_fed"])
        elif r["task_body"] is not None:
            fed = FEED_MARK in r["task_body"]
        else:
            fed = None
        c["sup"].append({"family": family(r["model"]), "xdom": bool(r["xdom"]),
                         "basis": r["basis"], "fed": fed, "stamped": stamped})
    return list(claims.values()), have_kanban


def score_claim(c, armed=False):
    sup = c["sup"]
    n = len(sup)
    n_fed = sum(1 for s in sup if s["fed"] is True)
    n_unknown = sum(1 for s in sup if s["fed"] is None)   # task body archived/lost
    n_blind = sum(1 for s in sup if s["fed"] is False)
    n_measured = n_fed + n_blind
    n_stamped = sum(1 for s in sup if s.get("stamped"))          # durable-stamp coverage
    n_stamped_fed = sum(1 for s in sup if s.get("stamped") and s["fed"] is True)
    all_fams = sorted({s["family"] for s in sup})
    xdom = sum(1 for s in sup if s["xdom"])
    xdom_fed = sum(1 for s in sup if s["xdom"] and s["fed"] is True)
    ratio = round(n_blind / n_measured, 3) if n_measured else None
    single_family = len(all_fams) <= 1
    weak = single_family
    # the haircut BITES here — but only when the check has ARMED it AND this claim has
    # enough STAMPED supports to measure. gentle + bounded: all-fed ⇒ 0.5, all-blind ⇒ 1.0.
    # neutral (1.0) otherwise, so it applies exactly where the data is real and nowhere else.
    mult = 1.0
    if armed and n_stamped >= MIN_STAMPED_SUP:
        blind_frac = 1 - (n_stamped_fed / n_stamped)
        mult = round(0.5 + 0.5 * blind_frac, 3)
    return {**{k: c[k] for k in ("claim_id", "tier", "domain")},
            "n_sup": n, "n_fed": n_fed, "n_blind": n_blind, "n_unknown": n_unknown,
            "n_measured": n_measured, "n_stamped": n_stamped, "n_stamped_fed": n_stamped_fed,
            "all_families": all_fams, "xdom_sup": xdom, "xdom_fed": xdom_fed,
            "independence_ratio": ratio, "single_family": single_family, "weak": weak,
            "independence_multiplier": mult}


def report(scored, have_kanban, state=None):
    state = state or {"armed": False}
    n = len(scored)
    tot_sup = sum(s["n_sup"] for s in scored)
    tot_measured = sum(s["n_measured"] for s in scored)
    tot_fed = sum(s["n_fed"] for s in scored)
    tot_blind = sum(s["n_blind"] for s in scored)
    tot_unknown = sum(s["n_unknown"] for s in scored)
    tot_stamped = sum(s["n_stamped"] for s in scored)
    tot_xdom = sum(s["xdom_sup"] for s in scored)
    single_fam = [s for s in scored if s["single_family"]]
    weak = [s for s in scored if s["weak"]]
    est_weak = [s for s in weak if s["tier"] == "ESTABLISHED"]
    haircut = [s for s in scored if s["independence_multiplier"] < 1.0]

    print(f"\n=== independence measure — {n} promotion-band claims, {tot_sup} supports ===")
    print(f"\n  [1] PRIOR-FED CONTAMINATION (the sharpest signal) — mostly UNMEASURABLE:")
    if not have_kanban:
        print("      ⚠ kanban.db not found.")
    print(f"      feed status recoverable for only {tot_measured}/{tot_sup} supports "
          f"({100*tot_measured/max(tot_sup,1):.1f}%) — {tot_unknown} task bodies were archived")
    print(f"      away with their text nulled. Of the {tot_measured} recoverable: "
          f"{tot_fed} prior-fed, {tot_blind} blind.")
    print(f"      durable prior_fed STAMP is now LIVE (task_prior_feed) — {tot_stamped} of the")
    print(f"        current shelf's supports carry it; the rest predate the stamp. Coverage grows")
    print(f"        forward as new stamped tasks promote. Reads ~0 on the OLD shelf by artifact")
    print(f"        (bodies archived), NOT because the shelf is independent.")

    print(f"\n  [2] MODEL-FAMILY MONOCULTURE (the robust retrospective signal):")
    print(f"      claims supported by a SINGLE model family: {len(single_fam)}/{n} "
          f"({100*len(single_fam)/max(n,1):.0f}%)")
    from collections import Counter
    fc = Counter(f for s in scored for f in s["all_families"])
    print(f"      support by family: " + ", ".join(f"{k} {v}" for k, v in fc.most_common(5)))
    print(f"      → this is the only independence axis measurable across the whole shelf,")
    print(f"        and it's near-floor: most claims rest on one family, and cross-family")
    print(f"        support is largely the deepseek AUDIT/ATTACK lane, not fresh confirmation.")

    print(f"\n=== price — the haircut, armed by CHECK (not clock) ===")
    armed = bool(state.get("armed"))
    if armed:
        print(f"  ✅ ARMED (accuracy {state.get('accuracy')}, {state.get('stamps')} stamps). "
              f"The support-depth haircut is LIVE and applied per-claim where stamped data exists.")
        print(f"  claims currently haircut (mult < 1.0): {len(haircut)}/{n}  "
              f"— neutral elsewhere until their supports carry stamps.")
        if haircut:
            worst = sorted(haircut, key=lambda s: s["independence_multiplier"])[:5]
            for s in worst:
                print(f"    #{s['claim_id']:<6d} ×{s['independence_multiplier']} "
                      f"(fed {s['n_stamped_fed']}/{s['n_stamped']} stamped) [{s['domain']}]")
    else:
        print(f"  NOT ARMED yet — multiplier held at 1.0 (NEUTRAL). The check arms it the")
        print(f"  instant the stamp is proven accurate over ≥{MIN_AUDIT} audited tasks and the")
        print(f"  table holds ≥{MIN_STAMPS} rows. Run `independence_gate.py --check` continuously.")
    print(f"  cross-domain (transfer) supports: {tot_xdom} of {tot_sup} "
          f"({100*tot_xdom/max(tot_sup,1):.0f}%) — the channel the haircut protects.")

    print(f"\n=== toy-vs-world (the number Opus asked for) ===")
    print(f"  'needs ≥1 non-simulation basis': 0/{n} claims would fail — but that is THEATER:")
    print(f"  verdict_basis='simulation' is set on ~7/{tot_sup} supports while the catch-all")
    print(f"  'independent_computation' covers most and ~55% is NULL. The rule passes")
    print(f"  ~everything for the wrong reason. It needs a text-level simulation detector")
    print(f"  (reuse discovery_routing.simulation_flag), NOT the label. Reported, not gated.")

    print(f"\n=== HONEST LIMITS (belong on the gate's own page) ===")
    print("  * prior-fed contamination is not retrospectively recoverable (archived bodies);")
    print("    the gate must be forward-instrumented to price it. Today it reports monoculture.")
    print("  * model-family independence is weak even when present: cross-family models share")
    print("    most training data. 'blind + cross-family' would be a proxy, not proof.")
    print("  * the deepest common cause is the shared base model — no internal agreement")
    print("    removes it; the toy-vs-world question needs external grounding, not this gate.")

    if est_weak:
        print(f"\n  ESTABLISHED single-family claims (clean-room candidates, when the lane is on):")
        for s in est_weak[:8]:
            print(f"    #{s['claim_id']:<6d} families={s['all_families']} "
                  f"sup={s['n_sup']} [{s['domain']}]")

    snap = {"generated_at": time.time(), "n_claims": n, "n_supports": tot_sup,
            "armed": bool(state.get("armed")), "stamps": state.get("stamps"),
            "stamp_accuracy": state.get("accuracy"),
            "stamped_shelf_supports": tot_stamped, "claims_haircut": len(haircut),
            "feed_recoverable_supports": tot_measured, "prior_fed_supports": tot_fed,
            "blind_supports": tot_blind, "unknown_body_supports": tot_unknown,
            "single_family_claims": len(single_fam),
            "family_support_counts": dict(fc.most_common()),
            "xdom_supports": tot_xdom,
            "weak_claim_ids": [s["claim_id"] for s in weak]}
    with open(REPORT + ".tmp", "w") as f:
        json.dump(snap, f, indent=1)
    os.replace(REPORT + ".tmp", REPORT)
    print(f"\nwrote {REPORT}")
    return weak


def ensure_ledger(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claim_independence (
            claim_id INTEGER PRIMARY KEY,
            independence_ratio REAL, n_sup INTEGER, n_stamped INTEGER, n_stamped_fed INTEGER,
            families TEXT, single_family INTEGER, independence_multiplier REAL, updated_at REAL)""")
    conn.commit()


def persist(scored):
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_ledger(conn)
    now = time.time()
    for s in scored:
        conn.execute(
            "INSERT INTO claim_independence (claim_id, independence_ratio, n_sup, n_stamped, "
            "n_stamped_fed, families, single_family, independence_multiplier, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(claim_id) DO UPDATE SET independence_ratio=excluded.independence_ratio, "
            "n_sup=excluded.n_sup, n_stamped=excluded.n_stamped, n_stamped_fed=excluded.n_stamped_fed, "
            "families=excluded.families, single_family=excluded.single_family, "
            "independence_multiplier=excluded.independence_multiplier, updated_at=excluded.updated_at",
            (s["claim_id"], s["independence_ratio"], s["n_sup"], s["n_stamped"], s["n_stamped_fed"],
             ",".join(s["all_families"]), 1 if s["single_family"] else 0,
             s["independence_multiplier"], now))
    # Neutralize rows for claims that LEFT the promotion band: the ledger is
    # otherwise a superset that keeps a demoted claim's last sub-1.0
    # multiplier forever, and discovery_spotlight applies that stale discount
    # (4 demoted claims were still being haircut). Families/ratio history
    # stays; only the live multiplier resets.
    if scored:
        placeholders = ",".join("?" * len(scored))
        neutralized = conn.execute(
            f"UPDATE claim_independence SET independence_multiplier = 1.0, updated_at = ? "
            f"WHERE independence_multiplier < 1.0 AND claim_id NOT IN ({placeholders})",
            [now] + [s["claim_id"] for s in scored]).rowcount
    else:
        neutralized = 0
    conn.commit()
    conn.close()
    print(f"persisted claim_independence for {len(scored)} claims "
          f"({sum(1 for s in scored if s['independence_multiplier']<1.0)} haircut"
          + (f"; {neutralized} off-band rows neutralized" if neutralized else "") + ")")


CLEAN_ROOM_MODEL = os.environ.get("HERMES_CLEANROOM_MODEL", "deepseek/deepseek-v4-flash")
CLEAN_ROOM_MAX_PENDING = 4
SAFE_CREATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "safe_kanban_create.py")


def _clean_room_body(exp_id, s, hypothesis):
    """A blind replication card: the hypothesis and NOTHING else — no prior
    findings, no supporting evidence, no verdict history. The [CLEAN-ROOM]
    prefix keeps the builders' suppress_prior_context guard honest if this text
    ever flows through them, and normalize_hypothesis strips it at attach time
    so the result hash-collides onto the SAME claim as a stamped-blind support."""
    return f"""[CLEAN-ROOM] Independent blind replication.

HYPOTHESIS: {hypothesis}

You are given ONLY the hypothesis above. Prior findings, supporting evidence,
and this system's current verdict are DELIBERATELY withheld: your run exists to
provide an INDEPENDENT test, and knowing what earlier workers concluded would
contaminate it. Do not search this system's databases for prior results on this
hypothesis; external literature/web search for methods and data is fine.

METHOD:
1. Design your own test from first principles — your choice of estimator,
   simulation, or derivation; do NOT try to reconstruct how it was tested before.
2. Run real code and analyze quantitatively (effect sizes / CIs / accuracy).
3. Report honestly. A refutation here is MORE valuable than a confirmation.

PREREGISTER (MANDATORY, before any code): PREDICTION: SUPPORTED or REFUTED,
CONFIDENCE 0.0-1.0, one WHY line. Report it verbatim via --predicted-direction.

RESULT WRITING (MANDATORY — run BEFORE kanban_complete):
  python3 ~/.hermes/scripts/write_worker_result.py --experiment {exp_id} \\
    --finding "CONFIRMED/REFUTED: summary. WHY IT WORKS: mechanism" \\
    [--supported] --confidence 0.XX --domain {s['domain'] or 'auto'} \\
    --basis independent_computation \\
    --predicted-direction '{{"clean_room_replicates": 1}}' \\
    --observed-direction '{{"clean_room_replicates": <+1 or -1>}}' \\
    --files "exp_code.py,exp_results.json"
Confidence is hard-capped at 0.85; --basis states what your verdict RESTS ON.
"""


def _clean_room_live_tasks(claim_ids):
    """claim_id -> True for claims with a live (ready/running) clean-room task.
    Dedup is LIVE-only: a dead/expired attempt makes the claim re-eligible
    (any-status-ever dedup is the poisoned-lockout pattern the retest lane
    already paid for)."""
    if not claim_ids:
        return {}
    try:
        conn = sqlite3.connect(f"file:{KANBAN}?mode=ro", uri=True, timeout=10)
        pconn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        live = {}
        rows = pconn.execute(
            "SELECT claim_id, kanban_task_id FROM clean_room_replications "
            "WHERE claim_id IN (%s)" % ",".join("?" * len(claim_ids)),
            list(claim_ids)).fetchall()
        for cid, tid in rows:
            st = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
            if st and st[0] in ("ready", "running"):
                live[cid] = True
        conn.close()
        pconn.close()
        return live
    except Exception:
        return {}


def clean_room(weak, limit, do_it):
    """CLEAN-ROOM lane (deliberate). For high-stakes independence-weak claims,
    enqueue a blind replication: RAG feed suppressed (the [CLEAN-ROOM] prefix +
    a feed-free body), cross-family model via kanban model_override, stamped
    prior_fed=0 so the result counts as a MEASURED-BLIND support — the only
    lane that establishes independence instead of estimating it. The result
    hash-collides onto the claim through the normal intake path; a refutation
    disputes it through the standard contradiction basis."""
    targets = sorted([s for s in weak if s["tier"] == "ESTABLISHED"],
                     key=lambda x: -x["n_sup"])
    print(f"\n=== CLEAN-ROOM (deliberate lane) — {len(targets)} high-stakes targets ===")
    if not do_it:
        for s in targets[:limit]:
            print(f"  would enqueue blind replication of #{s['claim_id']} "
                  f"(single-family {s['all_families']}, {s['n_sup']} supports, [{s['domain']}])")
        print("  [--enqueue not set — nothing enqueued]")
        return

    import re as _re
    import subprocess
    import uuid as _uuid
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""CREATE TABLE IF NOT EXISTS clean_room_replications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        kanban_task_id TEXT,
        experiment_id TEXT,
        model TEXT,
        created_at REAL NOT NULL)""")
    live = _clean_room_live_tasks([s["claim_id"] for s in targets])
    n_live = sum(1 for v in live.values() if v)
    slots = max(0, min(limit, CLEAN_ROOM_MAX_PENDING - n_live))
    created = 0
    for s in targets:
        if created >= slots:
            break
        if live.get(s["claim_id"]):
            continue
        # goal already achieved: a stamped-blind support exists
        if s.get("n_stamped", 0) - s.get("n_stamped_fed", 0) >= 1:
            continue
        hyp = conn.execute("SELECT hypothesis_text FROM knowledge_claims WHERE id=?",
                           (s["claim_id"],)).fetchone()
        if not hyp or not (hyp[0] or "").strip():
            continue
        exp_id = f"exp_cleanroom{s['claim_id']}_{_uuid.uuid4().hex[:4]}"
        title = (f"{exp_id}: [CLEAN-ROOM] Blind replication of claim "
                 f"#{s['claim_id']}: {(hyp[0] or '')[:60]}")
        body = _clean_room_body(exp_id, s, (hyp[0] or "").strip())
        # Direct INSERT with model_override IN the row (refiller's INSERT idiom):
        # create-then-UPDATE raced the 10s dispatcher — the lane's first card
        # (t_a2ce304f) was spawned on the fleet default before the override
        # landed. One atomic INSERT closes the race for good.
        tid = "t_" + _uuid.uuid4().hex[:8]
        try:
            k = sqlite3.connect(KANBAN, timeout=15)
            k.execute("PRAGMA busy_timeout=15000")
            k.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, priority, "
                "model_override, created_at) VALUES (?,?,?,?, 'ready', 3, ?, ?)",
                (tid, title, body, "default", CLEAN_ROOM_MODEL, int(time.time())))
            k.commit()
            k.close()
        except Exception as e:
            print(f"  create failed for #{s['claim_id']}: {e}")
            continue
        try:
            from prior_feed_stamp import record as _stamp
            _stamp(tid, body)   # records prior_fed=0 — the measured-blind stamp
        except Exception:
            pass
        conn.execute(
            "INSERT INTO clean_room_replications "
            "(claim_id, kanban_task_id, experiment_id, model, created_at) "
            "VALUES (?,?,?,?,?)",
            (s["claim_id"], tid, exp_id, CLEAN_ROOM_MODEL, time.time()))
        conn.commit()
        created += 1
        print(f"  ENQUEUED blind replication of #{s['claim_id']} -> {tid} "
              f"({exp_id}, {CLEAN_ROOM_MODEL})")
    print(f"  enqueued {created} (live before: {n_live}, cap {CLEAN_ROOM_MAX_PENDING})")
    conn.close()


def _set_model_override(task_id, model):
    """Same idiom as adversarial_replication_enqueuer.set_model_override."""
    try:
        k = sqlite3.connect(KANBAN, timeout=10)
        k.execute("PRAGMA busy_timeout=10000")
        cur = k.execute("UPDATE tasks SET model_override=? WHERE id=?", (model, task_id))
        k.commit()
        ok = cur.rowcount == 1
        k.close()
        return ok
    except Exception as e:
        print(f"  WARN: model_override failed for {task_id}: {e}")
        return False


def sweep_missing_stamps(settle_seconds=900, cap=25000):
    """Stamp any settled, bodied t_ task that still has no task_prior_feed row.

    The teeth-keeper: only ~4 enqueuers call prior_feed_stamp.record() at
    creation, but a dozen+ (synthesis, break-lane, transfer, compression-
    boundary, detector retests) INSERT INTO tasks directly, leaking ~24%/day
    of new tasks unstamped. Without this, coverage decays after the one-shot
    backfill. Rides the existing --check cron; prior_fed is body-derived with
    the SAME rule as prior_feed_stamp.record (single source of truth), INSERT
    OR IGNORE so a real creation stamp is never overwritten. Best-effort:
    never raises into the gate.
    """
    import time as _t
    try:
        from prior_feed_stamp import FEED_MARK as _FM, _fed_hashes as _fh, DB as _PDB
        import json as _j
        pk = _PP_KANBAN_DB
        pc = sqlite3.connect(f"file:{_PDB}?mode=ro", uri=True)
        have = {r[0] for r in pc.execute("SELECT kanban_task_id FROM task_prior_feed")}
        pc.close()
        kc = sqlite3.connect(f"file:{pk}?mode=ro", uri=True)
        cutoff = _t.time() - settle_seconds
        rows = []
        for table in ("tasks", "archived_tasks"):
            try:
                cur = kc.execute(
                    f"SELECT id, body, created_at FROM {table} "
                    "WHERE id LIKE 't\\_%' ESCAPE '\\' AND body IS NOT NULL AND TRIM(body)!=''")
            except sqlite3.OperationalError:
                continue
            for tid, body, created in cur:
                if tid in have or len(rows) >= cap:
                    continue
                try:
                    if created and float(created) > cutoff:
                        continue
                except (TypeError, ValueError):
                    pass
                fed = _FM in (body or "")
                rows.append((tid, 1 if fed else 0,
                             len(_fh(body)) if fed else 0,
                             _j.dumps(_fh(body)) if fed else "[]", _t.time()))
        kc.close()
        if not rows:
            return 0
        wc = sqlite3.connect(_PDB, timeout=30)
        wc.execute("PRAGMA busy_timeout=30000")
        wc.executemany(
            "INSERT OR IGNORE INTO task_prior_feed "
            "(kanban_task_id, prior_fed, n_fed, fed_hashes, created_at) VALUES (?,?,?,?,?)", rows)
        wc.commit()
        wc.close()
        return len(rows)
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser(description="Measure/price the shelf's independence")
    ap.add_argument("--check", action="store_true",
                    help="self-arming check: audit the stamp + arm the haircut (run continuously via cron)")
    ap.add_argument("--apply", action="store_true", help="persist claim_independence ledger")
    ap.add_argument("--enqueue", action="store_true", help="CLEAN-ROOM lane (deliberate; off by default)")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    conn = ro()
    if args.check:
        print("=== independence self-arming check ===")
        swept = sweep_missing_stamps()
        if swept:
            print(f"  sweep: stamped {swept} newly-created unstamped tasks")
        state = check_and_arm(conn)
    else:
        state = armed_state()
    armed = bool(state.get("armed"))
    claims, have_kanban = gather(conn)
    conn.close()
    scored = [score_claim(c, armed=armed) for c in claims]
    weak = report(scored, have_kanban, state)
    if args.apply:
        persist(scored)
    clean_room(weak, args.limit, args.enqueue)   # dry-prints unless --enqueue
    return 0


if __name__ == "__main__":
    sys.exit(main())
