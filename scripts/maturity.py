#!/usr/bin/env python3
"""
maturity.py — Computed maturity as a pure function of provenance signals.

Replaces the six-state-transition model (promote/demote) in
contradiction_detector.py with a single recompute-every-cycle approach.
Claim status is derived fresh from existing provenance signals on each
contradiction_detector.py cycle, not advanced through stored transitions.

Design:
  - MATURITY_THRESHOLDS: externalized epistemic policy (will evolve)
  - MaturityResult: structured return with passed/failed checks + blocking reason
  - compute_maturity(): pure function, no DB side effects
  - recompute_all_maturity(): reads all signals, calls compute_maturity,
    writes status + history row on change
  - recompute_n_formal_replications(): populates the new column

Status history is written to claim_status_history on every status change.
Legacy statuses (WELL_KNOWN, KNOWN, NOVEL, PARTIAL) are exempt from recompute.

claim_status NULL is a COMPUTED resting tier, not a coverage gap: recompute
runs over these every cycle (the selection includes claim_status IS NULL),
but compute_maturity legitimately returns None at the candidate gate for any
claim below candidate_wsc (0.5) — e.g. a claim whose only evidence is
refuting (hypothesis_supported=0/NULL, so weighted_support_count=0) or whose
sole support is FAILED-artifact (weight 0.3). Measured 2026-07-09: ~10,344
evidence-bearing claims correctly hold NULL; 0/10,344 reach wsc 0.5 and 0
carry a contradiction basis, so a "backfill" over them is a guaranteed no-op
that only burns a write window. This is the designed refuted/weak resting
state; do not add a stored REFUTED tier without redesigning the DISPUTED
basis and the retest lanes.
"""

import sys
import os
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db

# ---------------------------------------------------------------------------
# Epistemic policy — externalized thresholds
# ---------------------------------------------------------------------------
# These are the gate values for each maturity tier. They are policy, not
# implementation constants. When the system evolves (e.g., requiring 2
# challenges for ESTABLISHED), change the dict, not the CASE expression.

MATURITY_THRESHOLDS = {
    "candidate_wsc": 0.5,
    "replicated_wsc": 2.0,
    "replicated_retests": 1,
    "established_wsc": 3.0,
    "established_formal_replications": 1,
    "established_sa_max": 0.6,
    "disputed_contradiction_min": 1,
    # 2026-07-01: ESTABLISHED additionally requires surviving at least one
    # ADVERSARIAL replication (a break-lane task explicitly instructed to
    # refute the claim — see adversarial_replication_enqueuer.py). An
    # ordinary replication that re-derives the same construction replicates
    # the flaw; survival of a genuine attack is the bar for the top tier.
    "established_break_survivals": 1,
    # 2026-07-09: world gate (gate, not weight). A claim whose latest verified
    # world-grounding outcome is FAILS cannot reach ESTABLISHED (0 = any such
    # FAILS blocks). Binds only when ~/.hermes/world_gate_armed.json is armed.
    "established_world_fails_max": 0,
    # 2026-07-04: independence gate (gate, not weight). ESTABLISHED requires
    # >= 1 BLIND support — one whose task was durably stamped prior_fed=0
    # (task_prior_feed), i.e. the worker was NOT handed the RAG "CONFIRMED
    # PRIOR FINDINGS" feed. Agreement induced by feeding workers the
    # conclusion is not independent confirmation. Applies ONLY when (a) the
    # stamp has self-armed (independence_gate.py --check proves stamp
    # accuracy; independence_armed.json) and (b) the claim has >= 2 STAMPED
    # supports. NOTE (2026-07-09): the "pre-stamp claims are exempt — we cannot
    # retroactively know" rationale is OBSOLETE — kanban retains task bodies, so
    # prior_fed is recoverable from the body (FEED_MARK) at 99.99% agreement with
    # creation stamps; backfill_prior_feed_stamps.py stamped the pre-stamp shelf
    # and independence_gate.sweep_missing_stamps keeps it covered, so the gate now
    # binds shelf-wide, not just stamp-era. Active settlement paths: the retest lane
    # (blind since suppress_prior_context) and the clean-room lane both
    # produce stamped-blind supports — no one-way door.
    "established_blind_supports": 1,
    "blind_gate_min_stamped": 2,
}

# Legacy statuses that the recompute must NOT touch. These predate the
# maturity system and carry semantics the recompute doesn't model.
EXEMPT_STATUSES = {"WELL_KNOWN", "KNOWN", "NOVEL", "PARTIAL", "MERGED"}


# ---------------------------------------------------------------------------
# Structured maturity result
# ---------------------------------------------------------------------------

@dataclass
class MaturityResult:
    """Pure-function output: what tier this claim belongs in and why."""
    status: str  # 'ESTABLISHED', 'REPLICATED', 'CANDIDATE', 'DISPUTED', or None
    passed_checks: list = field(default_factory=list)
    failed_checks: list = field(default_factory=list)
    blocking_reason: str = None  # why it didn't reach the next tier


# ---------------------------------------------------------------------------
# Pure function
# ---------------------------------------------------------------------------

def compute_maturity(
    wsc: float,
    refute_count: int,
    contradiction_count: int,
    n_retests: int,
    n_formal_replications: int,
    spurious_agreement: float,
    thresholds: dict = None,
    circular_construction: int = None,
    method_code_mismatch: int = None,
    n_break_survivals: int = 0,
    n_blind_supports: int = 0,
    n_stamped_supports: int = 0,
    independence_armed: bool = False,
    world_refuted: int = 0,
    world_gate_armed: bool = False,
) -> MaturityResult:
    """Compute claim maturity from provenance signals. No DB side effects.

    Returns a MaturityResult with the tier and the check breakdown.
    The function evaluates every check and records pass/fail, so the
    result is always explainable — even for claims that don't reach
    any tier (they get status=None with all checks listed).
    """
    t = thresholds or MATURITY_THRESHOLDS
    passed = []
    failed = []
    blocking = None

    # Normalize None → 0 for numeric comparisons
    wsc = wsc or 0.0
    refute_count = refute_count or 0
    contradiction_count = contradiction_count or 0
    n_retests = n_retests or 0
    n_formal_replications = n_formal_replications or 0
    n_break_survivals = n_break_survivals or 0
    n_blind_supports = n_blind_supports or 0
    n_stamped_supports = n_stamped_supports or 0

    # --- DISPUTED: takes priority ---
    if contradiction_count >= t["disputed_contradiction_min"]:
        return MaturityResult(
            status="DISPUTED",
            passed_checks=[f"contradiction_count={contradiction_count} >= {t['disputed_contradiction_min']}"],
            failed_checks=[],
            blocking_reason="has contradictions",
        )

    # --- CIRCULAR CONSTRUCTION: caps at CANDIDATE (2026-07-01) ---
    # circularity_critic.py sets circular_construction = 1 when the supporting
    # experiments define their target from the same features used to detect it
    # (self-fulfilling by design — cannot fail, so agreement is meaningless).
    # Such claims stay CANDIDATE regardless of support volume until the flag
    # is cleared by a re-review with non-circular evidence.
    if circular_construction == 1 and wsc >= t["candidate_wsc"]:
        return MaturityResult(
            status="CANDIDATE",
            passed_checks=[f"wsc={wsc:.1f} >= {t['candidate_wsc']}"],
            failed_checks=["circular_construction = 1 (self-fulfilling design)"],
            blocking_reason="circular construction flagged by critic — capped at CANDIDATE",
        )

    # --- METHOD-CODE MISMATCH: caps at CANDIDATE (2026-07-07, ScientistOne CoE I4) ---
    # method_code_alignment_critic.py sets method_code_mismatch = 1 when a MAJORITY
    # of cross-family judges find the supporting code does not implement the method
    # the claim's own finding describes (prose says "bitwise encoding", code runs a
    # plain set). The numbers can be real and reproduce while the method is
    # misreported — irreproducible regardless of score — so the claim stays
    # CANDIDATE until re-reviewed against faithful code.
    if method_code_mismatch == 1 and wsc >= t["candidate_wsc"]:
        return MaturityResult(
            status="CANDIDATE",
            passed_checks=[f"wsc={wsc:.1f} >= {t['candidate_wsc']}"],
            failed_checks=["method_code_mismatch = 1 (described method != code)"],
            blocking_reason="method-code mismatch flagged by critic — capped at CANDIDATE",
        )

    # --- Evaluate all checks bottom-up ---
    # Candidate checks
    if wsc >= t["candidate_wsc"]:
        passed.append(f"wsc={wsc:.1f} >= {t['candidate_wsc']}")
    else:
        failed.append(f"wsc={wsc:.1f} < {t['candidate_wsc']}")
        return MaturityResult(
            status=None,
            passed_checks=passed,
            failed_checks=failed,
            blocking_reason=f"wsc too low for any tier (need >= {t['candidate_wsc']})",
        )

    refute_ok = refute_count == 0
    if refute_ok:
        passed.append("refutes = 0")
    else:
        failed.append(f"refute_count = {refute_count} (need 0)")

    contra_ok = contradiction_count == 0
    if contra_ok:
        passed.append("contradictions = 0")
    else:
        failed.append(f"contradiction_count = {contradiction_count} (need 0)")

    # --- Candidate (base tier) ---
    # Any claim with wsc >= 0.5 is at least CANDIDATE
    candidate_status = "CANDIDATE"
    candidate_passed = list(passed)
    candidate_failed = list(failed)

    # --- Replicated checks ---
    retest_ok = n_retests >= t["replicated_retests"]
    wsc_rep_ok = wsc >= t["replicated_wsc"]

    rep_passed = list(passed)
    rep_failed = list(failed)

    if wsc_rep_ok:
        rep_passed.append(f"wsc={wsc:.1f} >= {t['replicated_wsc']}")
    else:
        rep_failed.append(f"wsc={wsc:.1f} < {t['replicated_wsc']} (need >= {t['replicated_wsc']} for REPLICATED)")

    if retest_ok:
        rep_passed.append(f"n_retests={n_retests} >= {t['replicated_retests']}")
    else:
        rep_failed.append(f"n_retests={n_retests} < {t['replicated_retests']} (need >= {t['replicated_retests']} for REPLICATED)")

    can_replicate = wsc_rep_ok and refute_ok and contra_ok and retest_ok

    # --- Established checks ---
    wsc_est_ok = wsc >= t["established_wsc"]
    formal_rep_ok = n_formal_replications >= t["established_formal_replications"]
    sa_ok = spurious_agreement is None or spurious_agreement < t["established_sa_max"]
    break_ok = n_break_survivals >= t.get("established_break_survivals", 0)
    # Independence gate (2026-07-04): only binds when the stamp is armed and
    # the claim is stamp-covered; inert (blind_ok=True) otherwise.
    blind_gate_applies = (independence_armed
                          and n_stamped_supports >= t.get("blind_gate_min_stamped", 2))
    blind_ok = (not blind_gate_applies
                or n_blind_supports >= t.get("established_blind_supports", 1))
    # World gate (2026-07-09): a claim whose LATEST verified world-grounding
    # outcome is FAILS cannot reach ESTABLISHED until re-grounded. Gate, not
    # weight — caps the tier, never mutates wsc/posterior/refute_count (the
    # FAILS already flows as an ordinary refute through intake). Binds only
    # when armed (~/.hermes/world_gate_armed.json); inert otherwise, so the
    # disarmed code path is byte-identical to pre-gate behavior.
    world_ok = (not world_gate_armed) or (not world_refuted)

    est_passed = list(passed)
    est_failed = list(failed)

    if wsc_est_ok:
        est_passed.append(f"wsc={wsc:.1f} >= {t['established_wsc']}")
    else:
        est_failed.append(f"wsc={wsc:.1f} < {t['established_wsc']} (need >= {t['established_wsc']} for ESTABLISHED)")

    if formal_rep_ok:
        est_passed.append(f"n_formal_replications={n_formal_replications} >= {t['established_formal_replications']}")
    else:
        est_failed.append(f"n_formal_replications={n_formal_replications} < {t['established_formal_replications']} (need >= {t['established_formal_replications']} for ESTABLISHED)")

    if sa_ok:
        sa_val = spurious_agreement if spurious_agreement is not None else "NULL"
        est_passed.append(f"spurious_agreement={sa_val} < {t['established_sa_max']}")
    else:
        est_failed.append(f"spurious_agreement={spurious_agreement:.3f} >= {t['established_sa_max']} (false consensus)")

    if break_ok:
        est_passed.append(f"n_break_survivals={n_break_survivals} >= {t.get('established_break_survivals', 0)}")
    else:
        est_failed.append(f"n_break_survivals={n_break_survivals} < {t.get('established_break_survivals', 0)} (must survive an adversarial replication for ESTABLISHED)")

    if blind_gate_applies:
        if blind_ok:
            est_passed.append(f"n_blind_supports={n_blind_supports} >= {t.get('established_blind_supports', 1)} (stamped era)")
        else:
            est_failed.append(f"n_blind_supports={n_blind_supports} < {t.get('established_blind_supports', 1)} "
                              f"(all {n_stamped_supports} stamped supports were prior-fed — "
                              f"needs one blind confirmation; retest/clean-room lanes supply)")

    if world_gate_armed and world_refuted:
        est_failed.append("latest verified world-grounding is FAILS "
                          "(blocked from ESTABLISHED until re-grounded — world gate)")

    can_establish = (wsc_est_ok and refute_ok and contra_ok
                     and formal_rep_ok and sa_ok and break_ok and blind_ok and world_ok)

    # --- Determine status (highest tier that passes all checks) ---
    if can_establish:
        return MaturityResult(
            status="ESTABLISHED",
            passed_checks=est_passed,
            failed_checks=est_failed,
            blocking_reason=None,
        )

    if can_replicate:
        # Figure out what's blocking ESTABLISHED
        blockers = []
        if not wsc_est_ok:
            blockers.append(f"wsc needs {t['established_wsc']}")
        if not formal_rep_ok:
            blockers.append(f"needs {t['established_formal_replications']} formal replication(s)")
        if not sa_ok:
            blockers.append(f"spurious_agreement too high ({spurious_agreement:.3f})")
        if not break_ok:
            blockers.append(f"needs {t.get('established_break_survivals', 0)} adversarial-replication survival(s)")
        if not blind_ok:
            blockers.append("needs 1 blind (non-prior-fed) support")
        if not world_ok:
            blockers.append("blocked by verified world-grounding FAILS — needs re-grounding")
        blocking = "; ".join(blockers) if blockers else None
        return MaturityResult(
            status="REPLICATED",
            passed_checks=rep_passed,
            failed_checks=rep_failed,
            blocking_reason=blocking,
        )

    # CANDIDATE — figure out what's blocking REPLICATED
    blockers = []
    if not wsc_rep_ok:
        blockers.append(f"wsc needs {t['replicated_wsc']}")
    if not retest_ok:
        blockers.append(f"needs {t['replicated_retests']} independent retest(s)")
    if not refute_ok:
        blockers.append("has refutes")
    if not contra_ok:
        blockers.append("has contradictions")
    blocking = "; ".join(blockers) if blockers else None

    return MaturityResult(
        status="CANDIDATE",
        passed_checks=candidate_passed,
        failed_checks=candidate_failed,
        blocking_reason=blocking,
    )


# ---------------------------------------------------------------------------
# Recompute n_formal_replications
# ---------------------------------------------------------------------------

def recompute_n_independent_retests(conn):
    """Populate n_independent_retests for all claims.

    Counts completed replication_results rows (non-empty validation_finding)
    linked via claim_evidence → experiment — the same conservative join the
    original migration used (fix_evidence_integrity.py). Periodic because
    intake now writes replication_results whenever a candidate_retest task
    completes (apply_worker_results.py); before that the counter was frozen
    at whatever the one-shot migration computed.
    """
    conn.execute("""
        UPDATE knowledge_claims
        SET n_independent_retests = COALESCE((
            SELECT COUNT(DISTINCT rr.id)
            FROM claim_evidence ce
            JOIN replication_results rr ON rr.original_experiment_id = ce.experiment_id
            WHERE ce.claim_id = knowledge_claims.id
              AND rr.validation_finding IS NOT NULL AND rr.validation_finding != ''
        ), 0)
    """)


def recompute_n_formal_replications(conn):
    """Populate n_formal_replications for all claims.

    Counts replication_results rows with replication_status='replicated'
    linked via claim_evidence → experiment. Conservative join: broken
    evidence chains excluded. Mirrors the n_independent_retests pattern.
    """
    conn.execute("""
        UPDATE knowledge_claims
        SET n_formal_replications = COALESCE((
            SELECT COUNT(*)
            FROM claim_evidence ce
            JOIN replication_results rr ON rr.original_experiment_id = ce.experiment_id
            WHERE ce.claim_id = knowledge_claims.id
              AND rr.replication_status = 'replicated'
        ), 0)
    """)


# The live dispute basis, per claim. One term per DISPUTED writer, mirroring
# each writer's firing condition so the recompute never clears a dispute the
# writer would immediately re-add:
#   1. disagreed replications on the claim's first/last experiment
#      (contradiction_detector.detect_replication_disagreements)
#   2. min(3, refuting worker_results) when the claim's first experiment has
#      BOTH supporting and refuting results
#      (contradiction_detector.detect_worker_contradictions)
#   3. refuted adversarial replications (apply_worker_results block 1e)
#   4. min(3, incompatibility pairs) from contradictory answer adjudications
#      (answer_consistency_adjudicator.apply_contradiction) — zeroed once a
#      dispute_arbitrations row reaches a terminal non-expired status, which
#      is how arbitration outcomes stick under recompute.
_CONTRADICTION_BASIS_EXPR = """
      COALESCE((SELECT COUNT(*) FROM replication_results rr
                WHERE rr.replication_status = 'disagreed'
                  AND (rr.original_experiment_id = kc.first_experiment_id
                       OR rr.original_experiment_id = kc.last_experiment_id)), 0)
    + (CASE WHEN EXISTS (SELECT 1 FROM worker_results w1
                         WHERE w1.experiment_id = kc.first_experiment_id
                           AND w1.hypothesis_supported = 1)
             AND EXISTS (SELECT 1 FROM worker_results w0
                         WHERE w0.experiment_id = kc.first_experiment_id
                           AND w0.hypothesis_supported = 0)
       THEN MIN(3, (SELECT COUNT(*) FROM worker_results w0
                    WHERE w0.experiment_id = kc.first_experiment_id
                      AND w0.hypothesis_supported = 0))
       ELSE 0 END)
    + COALESCE((SELECT COUNT(*) FROM adversarial_replications ar
                WHERE ar.claim_id = kc.id AND ar.status = 'refuted'), 0)
    + (CASE WHEN EXISTS (SELECT 1 FROM dispute_arbitrations da
                         WHERE da.claim_id = kc.id
                           AND da.status NOT IN ('pending', 'expired'))
       THEN 0
       ELSE COALESCE((SELECT MIN(3, MAX(json_array_length(aa.incompatibilities)))
                      FROM answer_adjudications aa
                      WHERE aa.claim_id = kc.id
                        AND aa.consistent = 0
                        AND aa.incompatibilities IS NOT NULL
                        AND aa.incompatibilities != '[]'), 0) END)
"""


def recompute_contradiction_count(conn):
    """Recompute contradiction_count from the live dispute bases.

    contradiction_count was an increment-only ratchet across four writers,
    with only arbitration ever subtracting — so DISPUTED (which
    compute_maturity derives from count >= 1) was a one-way door: measured
    3,452 of 4,785 DISPUTED claims carried counts with no live basis of any
    kind. Deriving the count each cycle (the same treatment
    weighted_support_count and n_independent_retests already get) clears
    phantom disputes automatically and makes every future dispute self-heal
    the moment its basis is retracted or arbitrated.

    The writers' inline increments remain as immediate between-cycle marking;
    this recompute normalizes them to the live basis every cycle before the
    maturity recompute derives status. Update touches only rows whose value
    changes (full-table pass measured ~0.2s read side).
    """
    conn.execute(f"""
        UPDATE knowledge_claims AS kc
        SET contradiction_count = ({_CONTRADICTION_BASIS_EXPR})
        WHERE COALESCE(kc.contradiction_count, 0) != ({_CONTRADICTION_BASIS_EXPR})
    """)


# ---------------------------------------------------------------------------
# Main recompute loop
# ---------------------------------------------------------------------------

# Batch size for history inserts — commit every N claims to avoid
# holding a write lock for the full 61K+ claim table.
_COMMIT_BATCH = 500


def recompute_all_maturity(conn, dry_run=False, verbose=False):
    """Recompute claim_status for all non-exempt claims.

    For each claim:
      1. Read provenance signals from knowledge_claims
      2. Call compute_maturity() → MaturityResult
      3. If new_status != old_status, UPDATE claim_status and INSERT
         a history row into claim_status_history
      4. Commit every _COMMIT_BATCH claims

    Returns a dict with summary counts: {old_status: {new_status: count}}.
    """
    # Epoch float (2026-07-02 timestamp normalization): last_updated_at and
    # claim_status_history.changed_at were ISO strings, making numeric window
    # comparisons silently wrong (string > float is always true in SQLite).
    ts = time.time()

    # Ensure the new-signal columns/tables exist (2026-07-01) so the recompute
    # never crashes on a fresh DB or mid-rollout. Idempotent, metadata-only.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")]
        if "circular_construction" not in cols:
            conn.execute("ALTER TABLE knowledge_claims ADD COLUMN circular_construction INTEGER")
        if "method_code_mismatch" not in cols:
            conn.execute("ALTER TABLE knowledge_claims ADD COLUMN method_code_mismatch INTEGER")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS adversarial_replications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL,
                kanban_task_id TEXT,
                experiment_id TEXT,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending|survived|refuted|expired
                resolved_at REAL,
                notes TEXT
            )""")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_advrep_claim
            ON adversarial_replications(claim_id, status)""")
        # task_prior_feed normally created by prior_feed_stamp.record at task
        # creation; ensured here so the stamped-support subselects never crash
        # a fresh DB or mid-rollout recompute.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_prior_feed (
                kanban_task_id TEXT PRIMARY KEY,
                prior_fed INTEGER NOT NULL,
                n_fed INTEGER DEFAULT 0,
                fed_hashes TEXT,
                created_at REAL NOT NULL)""")
        # world_groundings normally created by world_grounding.ensure_ledger;
        # ensured here so the world-gate subselect never crashes a fresh DB.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS world_groundings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                outcome TEXT,
                verified INTEGER,
                resolved_at REAL)""")
        conn.commit()
    except Exception:
        pass

    # Independence gate arming: read once per pass. The gate binds only after
    # independence_gate.py --check has proven the prior_feed stamp accurate
    # (accuracy >= 0.98 over >= 50 audited, >= 500 stamps) — before that every
    # claim computes with the gate inert, byte-identical to pre-gate behavior.
    independence_armed = False
    try:
        import json as _json
        with open(os.path.expanduser("~/.hermes/independence_armed.json")) as _f:
            independence_armed = bool(_json.load(_f).get("armed"))
    except Exception:
        pass

    # World gate arming: read once per pass (2026-07-09). Binds only after an
    # operator arms it (measure-before-gating: >= ~20 verified world outcomes).
    # Disarmed → world_refuted never blocks, byte-identical to pre-gate.
    world_gate_armed = False
    try:
        import json as _json
        with open(os.path.expanduser("~/.hermes/world_gate_armed.json")) as _f:
            world_gate_armed = bool(_json.load(_f).get("armed"))
    except Exception:
        pass

    # Read all non-exempt claims with their signals. The two stamped-support
    # counts mirror recompute_weighted_support's definition of a counted
    # support (hypothesis_supported=1, non-retracted) joined to the durable
    # task_prior_feed stamp via worker_results.kanban_task_id.
    rows = conn.execute("""
        SELECT id, claim_status,
               COALESCE(weighted_support_count, 0) as wsc,
               COALESCE(refute_count, 0) as refute_count,
               COALESCE(contradiction_count, 0) as contradiction_count,
               COALESCE(n_independent_retests, 0) as n_retests,
               COALESCE(n_formal_replications, 0) as n_formal_replications,
               spurious_agreement,
               circular_construction,
               method_code_mismatch,
               COALESCE((SELECT COUNT(*) FROM adversarial_replications ar
                         WHERE ar.claim_id = knowledge_claims.id
                           AND ar.status = 'survived'), 0) as n_break_survivals,
               COALESCE((SELECT COUNT(*) FROM claim_evidence ce
                         JOIN worker_results wr ON wr.id = ce.worker_result_id
                         JOIN task_prior_feed tpf ON tpf.kanban_task_id = wr.kanban_task_id
                         WHERE ce.claim_id = knowledge_claims.id
                           AND wr.hypothesis_supported = 1
                           AND COALESCE(ce.evidence_type, 'support') != 'retracted_by_arbitration'
                        ), 0) as n_stamped_supports,
               COALESCE((SELECT COUNT(*) FROM claim_evidence ce
                         JOIN worker_results wr ON wr.id = ce.worker_result_id
                         JOIN task_prior_feed tpf ON tpf.kanban_task_id = wr.kanban_task_id
                         WHERE ce.claim_id = knowledge_claims.id
                           AND wr.hypothesis_supported = 1
                           AND COALESCE(ce.evidence_type, 'support') != 'retracted_by_arbitration'
                           AND tpf.prior_fed = 0
                        ), 0) as n_blind_supports,
               CASE WHEN COALESCE((SELECT wg.outcome FROM world_groundings wg
                         WHERE wg.claim_id = knowledge_claims.id
                           AND wg.status = 'resolved' AND wg.verified = 1
                           AND wg.outcome IN ('HOLDS', 'FAILS')
                         ORDER BY wg.resolved_at DESC LIMIT 1), '') = 'FAILS'
                    THEN 1 ELSE 0 END as world_refuted
        FROM knowledge_claims
        WHERE claim_status IS NULL
           OR claim_status NOT IN ('WELL_KNOWN', 'KNOWN', 'NOVEL', 'PARTIAL', 'MERGED')
    """).fetchall()

    transitions = {}  # {old_status: {new_status: count}}
    changes = 0
    processed = 0
    # Buffer writes and apply AFTER the scan. Writing inline opened a
    # transaction at the FIRST status change and held the write lock across
    # the rest of the ~70k-row pure-Python loop (seconds), starving every
    # concurrent writer. The scan reads a fetchall() snapshot, so deferring
    # the writes changes nothing about what is written.
    pending = []  # (update_params, history_params)

    for row in rows:
        claim_id = row["id"]
        old_status = row["claim_status"]
        wsc = row["wsc"]
        refute_count = row["refute_count"]
        contradiction_count = row["contradiction_count"]
        n_retests = row["n_retests"]
        n_formal_replications = row["n_formal_replications"]
        spurious_agreement = row["spurious_agreement"]

        result = compute_maturity(
            wsc=wsc,
            refute_count=refute_count,
            contradiction_count=contradiction_count,
            n_retests=n_retests,
            n_formal_replications=n_formal_replications,
            spurious_agreement=spurious_agreement,
            circular_construction=row["circular_construction"],
            method_code_mismatch=row["method_code_mismatch"],
            n_break_survivals=row["n_break_survivals"],
            n_blind_supports=row["n_blind_supports"],
            n_stamped_supports=row["n_stamped_supports"],
            independence_armed=independence_armed,
            world_refuted=row["world_refuted"],
            world_gate_armed=world_gate_armed,
        )

        new_status = result.status
        processed += 1

        # Only write if status actually changed
        if new_status != old_status:
            changes += 1
            old_key = old_status or "NULL"
            new_key = new_status or "NULL"
            transitions.setdefault(old_key, {})
            transitions[old_key][new_key] = transitions[old_key].get(new_key, 0) + 1

            if verbose:
                print(f"  Claim {claim_id}: {old_key} -> {new_key}"
                      f" | passed: {result.passed_checks}"
                      f" | failed: {result.failed_checks}"
                      f" | blocking: {result.blocking_reason}")

            if not dry_run:
                pending.append((
                    # Update claim status (NULL means no tier)
                    (new_status, ts, claim_id),
                    # History row (use 'NULL' string for None status)
                    (claim_id, ts, old_status or "NULL", new_status or "NULL",
                     result.blocking_reason,
                     wsc, n_retests, spurious_agreement, contradiction_count),
                ))

    # Apply the buffered writes in one short transaction (typically ~20 rows).
    for i, (upd, hist) in enumerate(pending, 1):
        conn.execute(
            "UPDATE knowledge_claims SET claim_status = ?, last_updated_at = ? WHERE id = ?",
            upd,
        )
        conn.execute("""
            INSERT INTO claim_status_history
            (claim_id, changed_at, old_status, new_status, blocking_reason,
             wsc_at_change, n_retests_at_change, sa_at_change,
             contradiction_count_at_change)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, hist)
        if i % _COMMIT_BATCH == 0:
            conn.commit()
    if pending:
        conn.commit()

    return {
        "processed": processed,
        "changes": changes,
        "transitions": transitions,
    }


# ---------------------------------------------------------------------------
# Standalone entry point (for dry-run testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Recompute claim maturity from provenance signals")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    parser.add_argument("--verbose", action="store_true", help="Print every transition")
    parser.add_argument("--recompute-replications", action="store_true",
                        help="Also recompute n_formal_replications before maturity recompute")
    args = parser.parse_args()

    conn = get_db()

    # Optionally recompute n_formal_replications first
    if args.recompute_replications:
        print("Recomputing n_formal_replications...")
        recompute_n_formal_replications(conn)
        conn.commit()
        print("Done.")

    # Capture before distribution
    before = {}
    for row in conn.execute(
        "SELECT COALESCE(claim_status, 'NULL') as s, COUNT(*) as c FROM knowledge_claims GROUP BY s"
    ).fetchall():
        before[row["s"]] = row["c"]

    print(f"\nBefore distribution ({sum(before.values())} total):")
    for status, count in sorted(before.items(), key=lambda x: -x[1]):
        print(f"  {status}: {count}")

    # Run recompute
    print(f"\nRunning recompute ({'dry-run' if args.dry_run else 'LIVE'})...")
    result = recompute_all_maturity(conn, dry_run=args.dry_run, verbose=args.verbose)

    print(f"\nProcessed: {result['processed']}")
    print(f"Changes: {result['changes']}")

    if result["transitions"]:
        print("\nTransitions:")
        for old, new_map in sorted(result["transitions"].items()):
            for new, count in sorted(new_map.items(), key=lambda x: -x[1]):
                print(f"  {old} → {new}: {count}")

    # After distribution (only meaningful for live runs)
    if not args.dry_run:
        after = {}
        for row in conn.execute(
            "SELECT COALESCE(claim_status, 'NULL') as s, COUNT(*) as c FROM knowledge_claims GROUP BY s"
        ).fetchall():
            after[row["s"]] = row["c"]

        print(f"\nAfter distribution ({sum(after.values())} total):")
        for status in sorted(set(list(before.keys()) + list(after.keys())), key=lambda x: -before.get(x, 0)):
            b = before.get(status, 0)
            a = after.get(status, 0)
            delta = a - b
            sign = "+" if delta > 0 else ""
            print(f"  {status}: {b} → {a} ({sign}{delta})")

    conn.close()
