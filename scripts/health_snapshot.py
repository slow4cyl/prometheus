#!/usr/bin/env python3
"""
health_snapshot.py — One-screen health board for Prometheus.

The point: glance at this, know whether the science loop is ALIVE and
WELL-CALIBRATED (internal validity only — the shelf measures agreement between
this system's runs, not correspondence to the world), without reverse-engineering
the system through one lineage sample.

Design principle (the thing the user and I worked out 2026-06-23):
  Lineage DEPTH is a thermometer, not the engine. The system selects for
  TRANSFERABLE MECHANISMS, not deep lineages. So depth is shown ALONGSIDE
  transfer-survival and mechanism signals — never alone — so a climbing depth
  number can't be mistaken for "all good" if transfer survival has cratered,
  and a flat depth number triggers a look at WHY (dispatch? gate?) rather than panic.

Usage:
    python3 ~/.hermes/scripts/health_snapshot.py          # full board
    python3 ~/.hermes/scripts/health_snapshot.py --json   # machine-readable
"""

import sqlite3
import os
import sys
import time
import json

DB = os.path.expanduser("~/.hermes/prometheus.db")
KANBAN = os.path.expanduser("~/.hermes/kanban.db")

NOW = int(time.time())
H1 = NOW - 3600
H6 = NOW - 6 * 3600
H24 = NOW - 24 * 3600


def q1(conn, sql, args=()):
    try:
        r = conn.execute(sql, args).fetchone()
        return r[0] if r else None
    except Exception:
        return None


def color(s, c):
    codes = {"g": "32", "y": "33", "r": "31", "b": "1", "dim": "2"}
    if not sys.stdout.isatty():
        return s
    return f"\033[{codes.get(c,'0')}m{s}\033[0m"


def status_dot(ok, warn=None):
    if ok:
        return color("●", "g")
    if warn:
        return color("●", "y")
    return color("●", "r")


def main():
    as_json = "--json" in sys.argv
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    M = {}  # metrics

    # ── THROUGHPUT: is the loop actually running? ──
    # NOTE: worker_results.created_at has ~3k corrupted TEXT values mixed in (finding
    # text leaked into the column). Filter to numeric types so comparisons are honest.
    _wr_time_ok = "typeof(created_at) IN ('integer','real')"
    M["experiments_1h"] = q1(conn, f"SELECT COUNT(*) FROM worker_results WHERE {_wr_time_ok} AND created_at > ?", (H1,)) or 0
    M["experiments_24h"] = q1(conn, f"SELECT COUNT(*) FROM worker_results WHERE {_wr_time_ok} AND created_at > ?", (H24,)) or 0
    M["exp_per_hr_24h"] = round(M["experiments_24h"] / 24.0, 1)

    # ── VERDICT MIX: confirm vs refute (refutation is healthy, not failure) ──
    sup = q1(conn, f"SELECT COUNT(*) FROM worker_results WHERE {_wr_time_ok} AND created_at > ? AND hypothesis_supported=1", (H24,)) or 0
    ref = q1(conn, f"SELECT COUNT(*) FROM worker_results WHERE {_wr_time_ok} AND created_at > ? AND hypothesis_supported=0", (H24,)) or 0
    tot = sup + ref
    M["confirm_rate_24h"] = round(100 * sup / tot, 1) if tot else None
    M["refute_rate_24h"] = round(100 * ref / tot, 1) if tot else None

    # ── TRANSFER SURVIVAL: the ACTUAL product (mechanisms surviving cross-domain) ──
    rep = q1(conn, "SELECT COUNT(*) FROM replication_results WHERE replication_status='replicated'") or 0
    dis = q1(conn, "SELECT COUNT(*) FROM replication_results WHERE replication_status='disagreed'") or 0
    rtot = rep + dis
    M["transfer_survival_rate"] = round(100 * rep / rtot, 1) if rtot else None
    M["transfers_completed_24h"] = q1(
        conn,
        "SELECT COUNT(*) FROM transfer_tracking WHERE status LIKE 'completed%' AND COALESCE(task_completed_at,created_at) > ?",
        (H24,),
    ) or 0
    M["cross_domain_transfers_total"] = q1(
        conn, "SELECT COUNT(*) FROM transfer_tracking WHERE status='completed_cross_domain'"
    ) or 0

    # ── MECHANISM ACCUMULATION: claims reaching real status ──
    # is_meta / is_empirical_fact split: the headline science tiers count
    # DISCOVERY claims only. Self-measurement (is_meta, transfer-rate
    # bookkeeping — meta_claim_classifier.py) and lookup-facts
    # (is_empirical_fact, named real-world events verified rather than
    # mechanisms discovered — empirical_fact_classifier.py) are reported
    # separately so "what has it discovered" is never answered by either.
    _NONSCI = "COALESCE(is_meta,0)=0 AND COALESCE(is_empirical_fact,0)=0"
    M["established"] = q1(conn, f"SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='ESTABLISHED' AND {_NONSCI}") or 0
    M["replicated"] = q1(conn, f"SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='REPLICATED' AND {_NONSCI}") or 0
    M["established_meta"] = q1(conn, "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='ESTABLISHED' AND is_meta=1") or 0
    M["replicated_meta"] = q1(conn, "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='REPLICATED' AND is_meta=1") or 0
    M["established_fact"] = q1(conn, "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='ESTABLISHED' AND is_empirical_fact=1") or 0
    M["replicated_fact"] = q1(conn, "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='REPLICATED' AND is_empirical_fact=1") or 0
    # knowledge_claims.created_at/last_updated_at hold uniform epoch values
    # since the 2026-07-02 normalization, BUT the columns are declared TEXT so
    # SQLite's affinity stores them as numeric-STRINGS ('1783032140.0').
    # Any numeric comparison MUST CAST — a bare TEXT-vs-number comparison is
    # always true and would count every claim as "new".
    M["new_claims_24h"] = q1(conn, "SELECT COUNT(*) FROM knowledge_claims WHERE CAST(created_at AS REAL) > ?", (H24,)) or 0

    # ── HONESTY: spurious agreement (false consensus) ──
    # Promotion-band only: SA gates ESTABLISHED, so the actionable number is
    # high-SA claims in the REPLICATED/ESTABLISHED band (the SA settlement
    # lane's intake). Counting all tiers made this a permanently-red vanity
    # gauge (~1300, mostly CANDIDATEs the retest lane already covers).
    M["high_spurious"] = q1(
        conn, "SELECT COUNT(*) FROM knowledge_claims "
        "WHERE spurious_agreement >= 0.6 AND COALESCE(is_meta,0)=0 "
        "AND claim_status IN ('REPLICATED','ESTABLISHED')") or 0
    M["high_spurious_all"] = q1(conn, "SELECT COUNT(*) FROM knowledge_claims WHERE spurious_agreement >= 0.6") or 0
    M["disputed"] = q1(conn, "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='DISPUTED'") or 0

    # ── EPISTEMIC PIPELINE: the gates that turn evidence into ESTABLISHED ──
    # (retest lane, dispute arbitration, adversarial attack gate). These
    # surface throughput of the machinery, so a silent regression — e.g. the
    # retest credit yield collapsing again — is visible instead of buried.
    def _q(sql, params=()):
        try:
            return q1(conn, sql, params)
        except Exception:
            return None
    # Retest gate: the throttle feeding REPLICATED. blocked = high-evidence
    # CANDIDATE claims stuck only because n_independent_retests=0.
    M["retest_gate_blocked"] = _q(
        "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='CANDIDATE' "
        "AND COALESCE(is_meta,0)=0 AND COALESCE(weighted_support_count,0)>=2.0 "
        "AND COALESCE(n_independent_retests,0)=0 AND COALESCE(refute_count,0)=0") or 0
    M["retest_credits_24h"] = _q(
        "SELECT COUNT(*) FROM replication_results WHERE selection_reason LIKE "
        "'candidate_retest%' AND validated_at > ?", (H24,)) or 0
    M["retest_credits_reconciled_24h"] = _q(
        "SELECT COUNT(*) FROM replication_results WHERE "
        "selection_reason='candidate_retest_reconciled' AND validated_at > ?", (H24,)) or 0
    # Dispute arbitration: is the settlement pipeline moving?
    M["arbitration_pending"] = _q(
        "SELECT COUNT(*) FROM dispute_arbitrations WHERE status='pending'") or 0
    M["arbitration_resolved_24h"] = _q(
        "SELECT COUNT(*) FROM dispute_arbitrations WHERE status NOT IN "
        "('pending','expired') AND resolved_at > ?", (H24,)) or 0
    # Adversarial attack gate: survival is the ESTABLISHED requirement.
    M["attacks_survived"] = _q("SELECT COUNT(*) FROM adversarial_replications WHERE status='survived'") or 0
    M["attacks_refuted"] = _q("SELECT COUNT(*) FROM adversarial_replications WHERE status='refuted'") or 0
    M["attacks_narrowed"] = _q("SELECT COUNT(*) FROM adversarial_replications WHERE status='narrowed'") or 0
    _atk_terminal = M["attacks_survived"] + M["attacks_refuted"]
    M["attack_survival_rate"] = (round(100 * M["attacks_survived"] / _atk_terminal, 1)
                                 if _atk_terminal else None)

    # Claim-scoping loop: NARROWED attacks spawn boundary questions; mapped
    # regimes land in claim_scopes; scoped re-attacks should converge to
    # survival (SURVIVED / SURVIVED_WITHIN_SCOPE) instead of re-narrowing.
    M["boundary_pool"] = _q(
        "SELECT COUNT(*) FROM curiosities WHERE provenance='narrowed_boundary' "
        "AND status='active'") or 0
    M["claim_scopes_total"] = _q("SELECT COUNT(*) FROM claim_scopes") or 0
    M["scoped_reattacks_survived"] = _q(
        "SELECT COUNT(*) FROM adversarial_replications ar "
        "WHERE ar.status='survived' AND EXISTS (SELECT 1 FROM claim_scopes cs "
        "WHERE cs.claim_id=ar.claim_id AND cs.created_at < ar.created_at)") or 0
    M["scoped_reattacks_narrowed"] = _q(
        "SELECT COUNT(*) FROM adversarial_replications ar "
        "WHERE ar.status='narrowed' AND EXISTS (SELECT 1 FROM claim_scopes cs "
        "WHERE cs.claim_id=ar.claim_id AND cs.created_at < ar.created_at)") or 0
    # PER-CLAIM convergence — the semantically honest gauge. The cumulative
    # attack-count totals above mix three populations and mislead: pre-terminal-cap
    # orbit debris (#66135 took 9 narrows in one day BEFORE the MAX_NARROWS cap
    # deployed, then stopped), cross-domain generalization probes (a NEW-domain
    # boundary is the map GROWING, not an orbit — sampled narrows are material,
    # quantified discoveries), and active mapping. What convergence actually means:
    # per claim, does the LATEST post-scope outcome eventually read survived?
    _conv = None
    try:
        _conv = conn.execute("""
            WITH scoped AS (
              SELECT ar.claim_id, ar.status, ar.created_at
              FROM adversarial_replications ar
              JOIN (SELECT claim_id, MIN(created_at) t0 FROM claim_scopes GROUP BY claim_id) s
                ON s.claim_id = ar.claim_id AND ar.created_at > s.t0
              WHERE ar.status='narrowed' OR ar.status LIKE 'survived%'),
            latest AS (
              SELECT claim_id,
                     (SELECT status FROM scoped s2 WHERE s2.claim_id = scoped.claim_id
                      ORDER BY created_at DESC LIMIT 1) last_status,
                     SUM(status='narrowed') n_narrow,
                     SUM(status LIKE 'survived%') n_surv
              FROM scoped GROUP BY claim_id)
            SELECT COUNT(*) total,
                   SUM(last_status LIKE 'survived%') converged,
                   SUM(last_status='narrowed' AND n_narrow < 3) mapping,
                   SUM(n_narrow >= 3 AND n_surv = 0) orbiting
            FROM latest""").fetchone()
    except Exception:
        pass
    M["scoped_claims_total"] = (_conv[0] or 0) if _conv else None
    M["scoped_claims_converged"] = (_conv[1] or 0) if _conv else None
    M["scoped_claims_mapping"] = (_conv[2] or 0) if _conv else None
    M["scoped_claims_orbiting"] = (_conv[3] or 0) if _conv else None
    # Unattacked REPLICATED: the attack lane's remaining intake.
    M["replicated_unattacked"] = _q(
        "SELECT COUNT(*) FROM knowledge_claims kc WHERE kc.claim_status='REPLICATED' "
        "AND COALESCE(kc.is_meta,0)=0 AND NOT EXISTS (SELECT 1 FROM "
        "adversarial_replications ar WHERE ar.claim_id=kc.id)") or 0
    # Drain-complete tripwire: when all three pools are ~empty the surge dials
    # should revert to steady state (see the drain-mode notes in the map).
    M["drain_complete"] = (
        (M.get("retest_gate_blocked") or 0) <= 5
        and M["boundary_pool"] <= 10
        and M["replicated_unattacked"] <= 5)

    # World-grounding verification lag: resolved HOLDS/FAILS outcomes the
    # mechanical verifier can't confirm (no preserved code / no declared
    # dataset / no external-I/O marker). world_grounding.reverify_unverified()
    # re-audits the whole tier every reconcile, so what remains is the honest
    # floor — some outcomes are permanently unverifiable and that is a truthful
    # label, not a defect. Gauge the SHARE, not the count: red means the lane
    # is resolving mostly-unverifiable outcomes (worker contract regressing),
    # not that the floor exists.
    M["world_unverified_stale"] = _q(
        "SELECT COUNT(*) FROM world_groundings WHERE status='resolved' "
        "AND outcome IN ('HOLDS','FAILS') AND COALESCE(verified,0)=0 "
        "AND COALESCE(resolved_at, 0) < ?", (NOW - 7 * 86400,))
    M["world_resolved_outcomes"] = _q(
        "SELECT COUNT(*) FROM world_groundings WHERE status='resolved' "
        "AND outcome IN ('HOLDS','FAILS')")

    # DB write-lock health: cron runs killed by "database is locked" in the
    # last 24h, counted from cron output logs. Tripwire for the 2026-07-02
    # write-path hardening (busy_timeout 30s + short transactions in intake/
    # detector): healthy is ~0; sustained double digits means a long-
    # transaction writer is back. Filenames encode local run time
    # (YYYY-MM-DD_HH-MM-SS.md) so recency is pruned WITHOUT stat calls.
    M["db_lock_failures_24h"] = None
    try:
        _out_root = os.path.expanduser("~/.hermes/cron/output")
        _cut = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(NOW - 86400))
        _hits = 0
        for _jd in os.scandir(_out_root):
            if not _jd.is_dir():
                continue
            for _f in os.scandir(_jd.path):
                _n = _f.name
                if not _n.endswith(".md") or _n[:-3] < _cut:
                    continue
                try:
                    with open(_f.path, errors="replace") as _fh:
                        if "database is locked" in _fh.read():
                            _hits += 1
                except OSError:
                    continue
        M["db_lock_failures_24h"] = _hits
    except Exception:
        pass

    # ── DEPTH (the thermometer — shown WITH context above, never alone) ──
    M["max_active_depth"] = q1(conn, "SELECT MAX(evidence_depth) FROM curiosities WHERE status='active'") or 0
    M["max_new_depth_6h"] = q1(conn, "SELECT MAX(evidence_depth) FROM curiosities WHERE created_at > ?", (H6,)) or 0
    M["new_deep_6h"] = q1(conn, "SELECT COUNT(*) FROM curiosities WHERE created_at > ? AND evidence_depth >= 10", (H6,)) or 0
    M["deepest_new_age_min"] = None
    row = conn.execute(
        "SELECT (? - MAX(created_at))/60 FROM curiosities WHERE evidence_depth >= 20", (NOW,)
    ).fetchone()
    if row and row[0] is not None:
        M["deepest_new_age_min"] = int(row[0])

    M["depth50_age_min"] = None
    row = conn.execute(
        "SELECT (? - MAX(created_at))/60 FROM curiosities WHERE evidence_depth >= 50", (NOW,)
    ).fetchone()
    if row and row[0] is not None:
        M["depth50_age_min"] = int(row[0])

    M["depth75_age_min"] = None
    row = conn.execute(
        "SELECT (? - MAX(created_at))/60 FROM curiosities WHERE evidence_depth >= 75", (NOW,)
    ).fetchone()
    if row and row[0] is not None:
        M["depth75_age_min"] = int(row[0])

    # ── QUEUE / DISPATCH: is the deep pipeline fed? ──
    M["active_curiosities"] = q1(conn, "SELECT COUNT(*) FROM curiosities WHERE status='active'") or 0
    conn.close()

    # kanban: deep tasks actually dispatched right now
    M["deep_tasks_dispatched"] = None
    try:
        k = sqlite3.connect(f"file:{KANBAN}?mode=ro", uri=True, timeout=10)
        k.execute(f"ATTACH 'file:{DB}?mode=ro' AS p")
        M["deep_tasks_dispatched"] = q1(
            k,
            "SELECT COUNT(*) FROM tasks t JOIN p.curiosities c "
            "ON c.id = CAST(SUBSTR(t.body, INSTR(t.body,'CURIOSITY_ID:')+13) AS INTEGER) "
            "WHERE t.body LIKE '%CURIOSITY_ID:%' AND c.evidence_depth >= 10 "
            "AND t.status IN ('ready','running')",
        )
        M["ready_tasks"] = q1(k, "SELECT COUNT(*) FROM tasks WHERE status='ready'")
        M["running_tasks"] = q1(k, "SELECT COUNT(*) FROM tasks WHERE status='running'")
        # Retest credit yield (24h): credits written / retest tasks completed.
        # Health target ~100% now that reconcile_retest_credits.py backstops
        # block 1g; a drop means the reconciler cron stalled. QUOTE-COLLIDERS are
        # excluded: any task whose title merely QUOTES a '[CANDIDATE-RETEST] ...'
        # string writes ordinary worker_results (or adversarial_replications),
        # never replication_results, so it can never credit and falsely tanks the
        # yield. Two collider generations so far: adversarial [RE-EXAMINE]/
        # [ADVERSARIAL] cards (read 60% off 4), then the REFUTED-branch follow-up
        # templates ("Which specific variable ... would flip the result of '...'"
        # / "What boundary conditions would cause '...' to fail?", read 73% off 3).
        # The durable invariant: a REAL retest task carries the tag in its title
        # HEAD ("[REFILLER] [CANDIDATE-RETEST] ..."), while every collider embeds
        # it deep inside a quoted string — so require tag position <= 40.
        _tag_head = ("((INSTR(title,'[CANDIDATE-RETEST]') BETWEEN 1 AND 40) "
                     "OR (INSTR(title,'[RETEST-GATE]') BETWEEN 1 AND 40))")
        _done = q1(k, f"SELECT COUNT(*) FROM tasks WHERE {_tag_head} "
                   "AND title NOT LIKE '%[RE-EXAMINE]%' "
                   "AND title NOT LIKE '%[ADVERSARIAL]%' AND status IN ('done','archived','completed') "
                   "AND completed_at > strftime('%s','now')-86400")
        M["retests_completed_24h"] = _done
        # Duplicate completions: retests of a source that already holds its
        # credit (written by an EARLIER task). They cannot earn a credit —
        # replication_results is UNIQUE(original_experiment_id) — so they must
        # not count against yield. Should be ~0 since the refiller title-dedup
        # fix; a rising count = the dedup regressed and the lane is burning
        # workers on re-runs.
        _dups = None
        try:
            _dups = q1(k, """
                SELECT COUNT(*) FROM tasks t
                JOIN p.curiosities c
                  ON c.id = CAST(SUBSTR(t.body, INSTR(t.body,'CURIOSITY_ID:')+13) AS INTEGER)
                JOIN p.replication_results rr ON rr.original_experiment_id = c.source_experiment
                WHERE ((INSTR(t.title,'[CANDIDATE-RETEST]') BETWEEN 1 AND 40)
                       OR (INSTR(t.title,'[RETEST-GATE]') BETWEEN 1 AND 40))
                  AND t.title NOT LIKE '%[RE-EXAMINE]%' AND t.title NOT LIKE '%[ADVERSARIAL]%'
                  AND t.status IN ('done','archived','completed')
                  AND t.completed_at > strftime('%s','now')-86400
                  AND t.body LIKE '%CURIOSITY_ID:%'
                  AND NOT EXISTS (SELECT 1 FROM p.replication_results r2
                                  WHERE r2.validation_task_id = t.id)""")
        except Exception:
            pass
        M["retest_dup_completions_24h"] = _dups
        # Yield = credited / creditable over the SAME completed-task set, so it
        # is bounded <=100 by construction. Two prior definitions failed: raw
        # credits/completions tanked to 58% during the drain (duplicates
        # inflated the denominator with tasks that could never earn a credit),
        # and credits-by-write-time over creditable-completions read 133%
        # (reconciler catch-up credits belong to tasks completed OUTSIDE the
        # window — numerator and denominator must share one clock).
        _credited = None
        try:
            _credited = q1(k, """
                SELECT COUNT(*) FROM tasks t
                WHERE ((INSTR(t.title,'[CANDIDATE-RETEST]') BETWEEN 1 AND 40)
                       OR (INSTR(t.title,'[RETEST-GATE]') BETWEEN 1 AND 40))
                  AND t.title NOT LIKE '%[RE-EXAMINE]%' AND t.title NOT LIKE '%[ADVERSARIAL]%'
                  AND t.status IN ('done','archived','completed')
                  AND t.completed_at > strftime('%s','now')-86400
                  AND EXISTS (SELECT 1 FROM p.replication_results r2
                              WHERE r2.validation_task_id = t.id)""")
        except Exception:
            pass
        M["retests_credited_24h"] = _credited
        _creditable = (_done - _dups) if (_done is not None and _dups is not None) else _done
        M["retest_credit_yield"] = (round(100 * _credited / _creditable, 0)
                                    if _creditable and _credited is not None else None)
        k.close()
    except Exception:
        M["ready_tasks"] = M["running_tasks"] = None
        M.setdefault("retests_completed_24h", None)
        M.setdefault("retest_credit_yield", None)

    if as_json:
        print(json.dumps(M, indent=2))
        return

    # ── RENDER ──
    def line(dot, label, val, note=""):
        # pad the value column on its VISIBLE length (color() adds invisible ANSI
        # codes that :<N padding miscounts, which was drifting the note column)
        v = str(val)
        print(f"  {dot} {label:<30} {color(v,'b')}{' ' * max(2, 24 - len(v))}{color(note,'dim')}")

    print()
    print(color("═" * 64, "dim"))
    print(color("  PROMETHEUS HEALTH SNAPSHOT", "b") + color(f"   {time.strftime('%Y-%m-%d %H:%M:%S')}", "dim"))
    print(color("═" * 64, "dim"))

    # 1. LOOP ALIVE
    print(color("\n  LOOP — is it running?", "b"))
    alive = M["experiments_1h"] > 20
    line(status_dot(alive, M["experiments_1h"] > 0), "experiments / last hr", M["experiments_1h"],
         "healthy >20/hr" if alive else "LOW — workers stalled?")
    line(color("·", "dim"), "experiments / hr (24h avg)", M["exp_per_hr_24h"], "")

    # 2. VERDICT MIX
    print(color("\n  VERDICTS — confirm vs refute (both healthy)", "b"))
    cr = M["confirm_rate_24h"]
    # 50-75% confirm is the healthy band; >90% = rubber-stamping, <30% = something off
    cr_ok = cr is not None and 30 <= cr <= 88
    line(status_dot(cr_ok, cr is not None), "confirm rate (24h)", f"{cr}%" if cr is not None else "n/a",
         "healthy 30-88%; >90% = rubber-stamp")
    line(color("·", "dim"), "refute rate (24h)", f"{M['refute_rate_24h']}%" if M['refute_rate_24h'] is not None else "n/a",
         "refutation drives mutation — good")

    # 3. TRANSFER SURVIVAL — the real product
    print(color("\n  TRANSFER SURVIVAL — the actual product", "b"))
    ts = M["transfer_survival_rate"]
    ts_ok = ts is not None and ts >= 50
    line(status_dot(ts_ok, ts is not None), "mechanism survival rate", f"{ts}%" if ts is not None else "n/a",
         "replicated / (replicated+disagreed)")
    line(color("·", "dim"), "cross-domain transfers (all)", M["cross_domain_transfers_total"], "")
    line(color("·", "dim"), "transfers completed (24h)", M["transfers_completed_24h"], "")

    # 4. MECHANISM ACCUMULATION
    print(color("\n  MECHANISMS — accumulating real claims?", "b"))
    line(status_dot(M["replicated"] > 0), "REPLICATED claims", M["replicated"],
         "discovery claims (excl. meta + empirical-fact)")
    line(color("·", "dim"), "ESTABLISHED claims", M["established"], "rare by design (strict gate)")
    _self_meas = M.get('replicated_meta', 0) + M.get('established_meta', 0)
    _lookups = M.get('replicated_fact', 0) + M.get('established_fact', 0)
    line(color("·", "dim"), "  held off the shelf", _self_meas + _lookups,
         f"{_self_meas} about-the-model + {_lookups} fact-lookups — matured, but not discoveries")
    line(color("·", "dim"), "new claims (24h)", M["new_claims_24h"], "")

    # 5. HONESTY
    print(color("\n  HONESTY — false consensus check", "b"))
    sp_ok = M["high_spurious"] < 50
    line(status_dot(sp_ok, True), "high spurious-agreement", M["high_spurious"],
         f"promotion-band false consensus (SA settlement lane intake; "
         f"{M.get('high_spurious_all', 0)} all tiers)")
    line(color("·", "dim"), "DISPUTED claims", M["disputed"], "contradictions caught — system working")

    # 5b. EPISTEMIC GATES — the machinery turning evidence into ESTABLISHED
    print(color("\n  GATES — retest / arbitration / attack throughput", "b"))
    yld = M.get("retest_credit_yield")
    yld_ok = yld is None or yld >= 85
    line(status_dot(yld_ok, yld is not None),
         "retest credit yield (24h)", f"{yld:.0f}%" if yld is not None else "n/a",
         "credits/creditable-completions — <85% = 1g leaking, cron stalled?")
    line(color("·", "dim"), "retest credits (24h)", M.get("retest_credits_24h", 0),
         f"{M.get('retest_credits_reconciled_24h', 0)} via reconciler safety-net")
    dup24 = M.get("retest_dup_completions_24h")
    if dup24 is not None:
        done24 = M.get("retests_completed_24h") or 0
        dup_ok = done24 == 0 or dup24 <= 0.1 * done24
        line(status_dot(dup_ok, True),
             "retest dup completions (24h)", dup24,
             "re-runs of already-credited claims — >10% of completions = refiller dedup regressed")
    line(color("·", "dim"), "retest-gate blocked", M.get("retest_gate_blocked", 0),
         "high-wsc CANDIDATEs awaiting a retest (drains over time)")
    line(color("·", "dim"), "arbitration (24h)",
         f"{M.get('arbitration_pending', 0)} waiting, {M.get('arbitration_resolved_24h', 0)} settled",
         "disputed claims getting a decisive tie-breaker experiment")
    asr = M.get("attack_survival_rate")
    line(color("·", "dim"), "attack survival rate",
         f"{asr}%" if asr is not None else "n/a",
         f"{M.get('attacks_survived',0)} survived / {M.get('attacks_refuted',0)} broke / "
         f"{M.get('attacks_narrowed',0)} narrowed")
    line(color("·", "dim"), "boundary pool / scopes mapped",
         f"{M.get('boundary_pool', 0)} / {M.get('claim_scopes_total', 0)}",
         "open boundary questions / regimes in claim_scopes")
    _ss, _sn = M.get("scoped_reattacks_survived", 0), M.get("scoped_reattacks_narrowed", 0)
    _scoped_total = _ss + _sn
    # Per-claim convergence is the honest read: the raw held/narrowed totals mix
    # pre-terminal-cap orbit debris + cross-domain probes whose narrows are the
    # map GROWING (material new-domain boundaries), so they sit near parity even
    # when the loop is healthy. A claim converges when its LATEST scoped outcome
    # is survived; orbiting = >=3 post-scope narrows and no survival ever.
    _sc_tot = M.get("scoped_claims_total") or 0
    _sc_conv = M.get("scoped_claims_converged") or 0
    _sc_orbit = M.get("scoped_claims_orbiting") or 0
    _scoped_ok = _sc_tot < 10 or (_sc_conv >= 0.5 * _sc_tot and _sc_orbit <= 0.1 * _sc_tot)
    line(status_dot(_scoped_ok, _sc_tot > 0),
         "scoped-claim convergence",
         f"{_sc_conv}/{_sc_tot} converged, {M.get('scoped_claims_mapping') or 0} mapping, {_sc_orbit} orbiting",
         f"latest scoped outcome survived / still mapping / >=3 narrows no survival ({_ss} held vs {_sn} narrowed raw)")
    # Independence gate: self-arms once stamps prove out, but the WEIGHT only bites
    # when shelf supports are actually stamped. Armed with 0 haircut = the stamps
    # aren't joining the shelf's (archived-body) supports — present but toothless.
    _ind_armed = False
    try:
        _ap = os.path.expanduser("~/.hermes/independence_armed.json")
        if os.path.exists(_ap):
            _ind_armed = bool(json.load(open(_ap)).get("armed"))
    except Exception:
        pass
    # Source of truth = the gate's own report (independence_gate.report():
    # claims_haircut over the CURRENT promotion band, atomic-rename write,
    # refreshed every 10m by the independence-check cron). The
    # claim_independence ledger is a SUPERSET — demoted claims keep their last
    # multiplier forever, so COUNT(mult<1.0) there overstates the bite (138 vs
    # 134 on 2026-07-09) and, worse, a dead cron would freeze the count green.
    # Read the report + a freshness guard instead.
    _ind_haircut, _ind_fresh = 0, False
    try:
        with open(os.path.expanduser("~/.hermes/independence_report.json")) as _rf:
            _rp = json.load(_rf)
        _ind_haircut = int(_rp.get("claims_haircut") or 0)
        _ind_fresh = (NOW - float(_rp.get("generated_at") or 0)) < 3600
    except Exception:
        pass
    if not _ind_armed:
        _ind_dot = color("·", "dim")
    elif _ind_haircut > 0 and _ind_fresh:
        _ind_dot = status_dot(True)
    else:
        _ind_dot = status_dot(False, True)   # armed but toothless OR report stale → yellow
    line(_ind_dot, "independence discount",
         (f"on, {_ind_haircut} flagged" + ("" if _ind_fresh else " (report STALE >1h)"))
         if _ind_armed else "warming up",
         "discounts claims propped up by the feed, not independent tests")
    _wus = M.get("world_unverified_stale")
    _wro = M.get("world_resolved_outcomes") or 0
    _wus_ok = _wus is not None and (_wro == 0 or _wus <= 0.4 * _wro)
    line(status_dot(_wus_ok, _wus is not None),
         "world outcomes unverified >7d",
         f"{_wus}/{_wro}" if _wus is not None else "n/a",
         "mechanically unverifiable share — gate binds on verified only; >40% = worker contract slipping")
    if M.get("drain_complete"):
        line(color("●", "y"), "DRAIN COMPLETE", "revert surge dials",
             "retest 18->8 p3->p2 · boundary 12->4 cap 16->8 · attack MAX_PENDING 16->3 p4->p3 · arb 8->4 p4->p3")
    lock24 = M.get("db_lock_failures_24h")
    line(status_dot(lock24 is not None and lock24 <= 5, lock24 is not None),
         "db lock failures (24h)", lock24 if lock24 is not None else "n/a",
         "cron runs killed by 'database is locked' — >5/day = long-txn writer is back")

    # 6. DEPTH (thermometer, read in context of the above)
    print(color("\n  DEPTH — lineage thermometer (read WITH transfer above)", "b"))
    climbing = M["max_new_depth_6h"] >= 10
    line(status_dot(climbing, M["new_deep_6h"] > 0), "max NEW depth (6h)", M["max_new_depth_6h"],
         "is the climb live? flat = dispatch/gate issue")
    line(color("·", "dim"), "new deep curiosities (6h)", M["new_deep_6h"], "depth>=10 created last 6h")
    line(color("·", "dim"), "max active depth (all)", M["max_active_depth"], "")
    age = M["deepest_new_age_min"]
    line(color("·", "dim"), "last depth-20+ born", f"{age}m ago" if age is not None else "never",
         "drives the dashboard lineage sample")
    age50 = M["depth50_age_min"]
    line(color("·", "dim"), "last depth-50+ born", f"{age50}m ago" if age50 is not None else "never",
         "")
    age75 = M["depth75_age_min"]
    line(color("·", "dim"), "last depth-75+ born", f"{age75}m ago" if age75 is not None else "never",
         "")

    # 7. DISPATCH PIPELINE
    print(color("\n  PIPELINE — is the deep climb fed?", "b"))
    dd = M["deep_tasks_dispatched"]
    dd_ok = dd is not None and dd > 0
    line(status_dot(dd_ok, True), "deep tasks dispatched now", dd if dd is not None else "n/a",
         "depth>=10 in ready/running — 0 = climb starved")
    line(color("·", "dim"), "ready / running tasks", f"{M['ready_tasks']} / {M['running_tasks']}", "")
    line(color("·", "dim"), "active curiosities", M["active_curiosities"], "")

    # ── ONE-LINE VERDICT ──
    print(color("\n" + "─" * 64, "dim"))
    problems = []
    if not alive:
        problems.append("loop slow")
    if ts is not None and ts < 50:
        problems.append("transfer survival low")
    if cr is not None and cr > 90:
        problems.append("confirm rate too high (rubber-stamping)")
    if dd == 0:
        problems.append("deep climb starved (no deep tasks dispatched)")
    if M["max_new_depth_6h"] < 10 and M["new_deep_6h"] == 0:
        problems.append("depth flat (check dispatch/gate)")
    if problems:
        print("  " + color("⚠ ATTENTION: ", "y") + color("; ".join(problems), "y"))
    else:
        print("  " + color("✓ Science loop alive, transferring, well-calibrated (internal validity only).", "g"))
    print(color("─" * 64, "dim"))
    print()


if __name__ == "__main__":
    main()
