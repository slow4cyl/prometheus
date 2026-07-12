#!/usr/bin/env python3
"""
contradiction_detector.py — Find contradictory evidence and update claim_status.

Runs every 5 minutes via cron.

Two detection paths:
  1. replication_results table: entries with replication_status='disagreed'
     → knowledge_claims.claim_status = 'DISPUTED'
  2. worker_results with opposite hypothesis_supported on same/similar hypothesis
     → marks the claim as DISPUTED

Also promotes CANDIDATE → REPLICATED when support_count >= 2 and no contradictions.

Usage:
    python3 ~/.hermes/scripts/contradiction_detector.py [--dry-run]
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from db_retry import get_db


def detect_replication_contradictions(conn, dry_run=False):
    """Path 1: Find disagreed replications and mark claims as DISPUTED.

    replication_results keys on EXPERIMENTS, so a fragment-merge (which
    re-points claim-keyed provenance only) leaves disagreed rows attached to
    MERGED tombstones' experiments. The join resolves those through
    ``merged_into`` so the SURVIVOR inherits the disagreement — same
    question, its replication disagreed — while the tombstone itself is
    never re-statused (survivors are never MERGED; the merge verifies
    no chains).
    """
    disagreed = conn.execute("""
        SELECT rr.original_experiment_id, rr.validation_experiment_id,
               rr.original_finding, rr.validation_finding,
               kc.id as claim_id, kc.hypothesis_text, kc.claim_status
        FROM replication_results rr
        JOIN knowledge_claims kc0 ON kc0.first_experiment_id = rr.original_experiment_id
                                  OR kc0.last_experiment_id = rr.original_experiment_id
        JOIN knowledge_claims kc ON kc.id = CASE
                 WHEN kc0.claim_status = 'MERGED' THEN kc0.merged_into
                 ELSE kc0.id END
        WHERE rr.replication_status = 'disagreed'
          AND (kc.claim_status IS NULL OR kc.claim_status NOT IN ('DISPUTED', 'ESTABLISHED', 'MERGED'))
    """).fetchall()

    updated = 0
    for row in disagreed:
        claim_id = row["claim_id"]
        if dry_run:
            print(f"[DRY-RUN] Would DISPUTE claim {claim_id}: "
                  f"'{row['hypothesis_text'][:80]}...' "
                  f"(original={row['original_experiment_id']} disagreed by {row['validation_experiment_id']})")
            updated += 1
            continue
        
        conn.execute(
            "UPDATE knowledge_claims SET claim_status = 'DISPUTED', "
            "contradiction_count = contradiction_count + 1, "
            "last_updated_at = ? "
            "WHERE id = ?",
            (time.time(), claim_id)
        )
        print(f"DISPUTED claim {claim_id}: '{row['hypothesis_text'][:80]}...' "
              f"(disagreed replication: {row['validation_experiment_id']})")
        updated += 1

    return updated


def detect_worker_contradictions(conn, dry_run=False):
    """Path 2: Find same-experiment worker_results with opposite conclusions.

    Uses first_experiment_id in knowledge_claims to find pairs where
    the same hypothesis has both supported=1 and supported=0 results.
    """
    contradictory_claims = conn.execute("""
        SELECT kc.id as claim_id, kc.hypothesis_text, kc.claim_status,
               COUNT(CASE WHEN wr1.hypothesis_supported = 1 THEN 1 END) as wr_support,
               COUNT(CASE WHEN wr1.hypothesis_supported = 0 THEN 1 END) as wr_refute
        FROM knowledge_claims kc
        JOIN worker_results wr1 ON wr1.experiment_id = kc.first_experiment_id
        WHERE kc.claim_status IS NULL
           OR kc.claim_status NOT IN ('DISPUTED', 'ESTABLISHED', 'MERGED')
        GROUP BY kc.id
        HAVING wr_support > 0 AND wr_refute > 0
    """).fetchall()

    updated = 0
    for row in contradictory_claims:
        claim_id = row["claim_id"]
        if dry_run:
            print(f"[DRY-RUN] Would DISPUTE claim {claim_id}: "
                  f"'{row['hypothesis_text'][:80]}...' "
                  f"(supports={row['wr_support']}, refutes={row['wr_refute']})")
            updated += 1
            continue
        
        conn.execute(
            "UPDATE knowledge_claims SET claim_status = 'DISPUTED', "
            "contradiction_count = contradiction_count + ?, "
            "last_updated_at = ? "
            "WHERE id = ?",
            (min(row["wr_refute"], 3),  # cap at 3 to avoid runaway counts
             time.time(), claim_id)
        )
        print(f"DISPUTED claim {claim_id}: '{row['hypothesis_text'][:80]}...' "
              f"({row['wr_support']} supports vs {row['wr_refute']} refutes)")
        updated += 1

    return updated


def recompute_weighted_support(conn):
    """Recompute weighted_support_count for all claims (P3 fix, June 17 2026).

    weighted_support_count = SUM of per-evidence weights:
      VERIFIED / VERIFIED_PARTIAL / DEPLOYED = 1.0
      UNVERIFIED                              = 0.7
      FAILED                                  = 0.3
      NULL / other                            = 0.5

    Only counts evidence where the worker_result hypothesis_supported = 1
    (i.e., evidence that actually SUPPORTS the claim, not refutes or unknowns).

    Consequence: a pure-refuted claim (evidence exists but none supports) gets
    weighted_support_count=0 and so holds claim_status NULL permanently — that
    is the designed refuted resting state, not an unscored backlog. See
    maturity.py's module docstring (the ~10,344 evidence-bearing NULL claims).
    """
    conn.execute("""
        UPDATE knowledge_claims
        SET weighted_support_count = COALESCE((
            SELECT SUM(
                CASE 
                    WHEN wr.artifact_status IN ('VERIFIED', 'VERIFIED_PARTIAL', 'DEPLOYED') THEN 1.0
                    WHEN wr.artifact_status = 'FILES_MISSING' THEN 0.7
                    WHEN wr.artifact_status = 'FAILED' THEN 0.3
                    ELSE 0.5
                END
            )
            FROM claim_evidence ce
            JOIN worker_results wr ON wr.id = ce.worker_result_id
            WHERE ce.claim_id = knowledge_claims.id
            AND wr.hypothesis_supported = 1
            AND COALESCE(ce.evidence_type, 'support') != 'retracted_by_arbitration'
        ), 0.0)
        WHERE COALESCE(claim_status,'') != 'MERGED'
    """)


def inject_retest_curiosities(conn, dry_run=False):
    """RETIRED — no longer called from run(). A REPLICATED claim already holds
    its replication_results credit and the table is UNIQUE per source
    experiment, so this lane's completions could write nothing; the
    adversarial attack gate is the disconfirmation lane for REPLICATED claims.

    Original purpose: one-time promotion gate: inject a disconfirmation task
    for newly-REPLICATED claims that have no completed retest.

    This replaces the old adversarial system's continuous loop with a single
    low-priority check per claim. When a claim crosses the REPLICATED threshold
    for the first time (wsc >= 2, no refutes, retest gate cleared), and it has
    NO completed retest (replication_results row with non-NULL validation_finding),
    inject one disconfirmation curiosity into the queue.

    Design constraints that prevent the old failure:
    - One task per claim, ever. Checked by provenance='retest_gate' — if a
      curiosity already exists for this claim, skip.
    - Low priority (2). Runs when the queue is shallow, not ahead of exploration.
    - Not a recurring check. The claim triggers exactly one check at the
      REPLICATED transition point.

    If the worker returns 'replicated', the claim is confirmed.
    If 'disagreed', the claim goes to DISPUTED.
    If the worker doesn't complete it, the claim sits without advancing.
    """
    ts = time.time()

    # Find REPLICATED claims with no completed retest
    needs_retest = conn.execute("""
        SELECT kc.id, kc.hypothesis_text, kc.first_experiment_id
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'REPLICATED'
          AND COALESCE(kc.n_independent_retests, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM curiosities c
              WHERE c.provenance = 'retest_gate'
                AND c.source_experiment = kc.first_experiment_id
          )
    """).fetchall()

    injected = 0
    for row in needs_retest:
        hypothesis = row['hypothesis_text']
        if not hypothesis:
            continue

        disconfirmation_text = (
            f"[RETEST-GATE] Attempt to DISCONFIRM: {hypothesis[:200]}\n"
            f"Design an experiment that would FAIL if this claim is a fluke, "
            f"artifact, or overgeneralization. Try different parameters, edge "
            f"cases, or alternative interpretations. If you cannot break the "
            f"claim despite genuine effort, report SUPPORTED. If you can break "
            f"it, report REFUTED with evidence of the failure condition."
        )

        if dry_run:
            print(f"[DRY-RUN] Would inject retest curiosity for claim {row['id']}: "
                  f"'{hypothesis[:60]}...'")
            injected += 1
            continue

        conn.execute("""
            INSERT INTO curiosities
            (text, priority, status, source_experiment, created_at, provenance)
            VALUES (?, 2, 'active', ?, ?, 'retest_gate')
        """, (disconfirmation_text, row['first_experiment_id'], ts))
        injected += 1
        print(f"INJECTED retest curiosity for claim {row['id']}: "
              f"'{hypothesis[:60]}...'")

    return injected


def inject_candidate_retests(conn, dry_run=False, quota=5):
    """Inject retest curiosities for CANDIDATE claims blocked at the retest gate.

    inject_retest_curiosities() targets REPLICATED claims with no retests — under
    computed maturity that population is always zero (such claims are demoted to
    CANDIDATE). This function targets the actual backlog: CANDIDATE claims with
    wsc >= 2.0 that are blocked only by the retest gate (n_independent_retests = 0).

    One ACTIVE retest curiosity per claim at a time. Ordered by
    weighted_support_count DESC so the highest-evidence claims get retested
    first. Capped at quota per cycle.

    The dedup deliberately counts only status='active' curiosities. A retest
    that actually ran earns a replication_results credit and sets
    n_independent_retests >= 1, which the main WHERE already excludes — so a
    non-active prior curiosity for a still-blocked claim means the retest
    never happened (self-resolved pre-guard, age-retired, any future
    closer-without-credit). The old any-status-ever dedup turned each such
    close into a permanent lockout: one dead curiosity and the claim could
    never be retested again (409 retired curiosities had locked out 345
    claims). Active-only dedup makes every closer-without-credit self-healing:
    the claim simply becomes re-injectable.
    """
    ts = time.time()

    candidates = conn.execute("""
        SELECT kc.id, kc.hypothesis_text, kc.first_experiment_id,
               kc.weighted_support_count
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'CANDIDATE'
          AND COALESCE(kc.weighted_support_count, 0) >= 2.0
          AND COALESCE(kc.n_independent_retests, 0) = 0
          AND COALESCE(kc.is_meta, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM curiosities c
              WHERE c.provenance = 'candidate_retest'
                AND c.source_experiment = kc.first_experiment_id
                AND c.status = 'active'
          )
        ORDER BY kc.weighted_support_count DESC
        LIMIT ?
    """, (quota,)).fetchall()

    injected = 0
    for row in candidates:
        hypothesis = row['hypothesis_text']
        if not hypothesis:
            continue

        # Strip [TRANSFER from ...] prefix so the lane filter routes to genuine,
        # not transfer. The hypothesis text often starts with [TRANSFER from X]
        # which causes the task_refiller to route it to the transfer lane (small
        # budget, ~2 tasks/cycle). The genuine lane has much larger budget.
        import re
        clean_hypothesis = re.sub(r'^\[TRANSFER from [^\]]+\]\s*', '', hypothesis)

        retest_text = (
            f"[CANDIDATE-RETEST] Replicate and validate: {clean_hypothesis[:200]}\n"
            f"This claim has weighted_support_count={row['weighted_support_count']:.1f} "
            f"but no independent retest. Run a controlled replication experiment: "
            f"same hypothesis, different execution. If your experiment supports the "
            f"claim, report SUPPORTED. If it contradicts, report REFUTED with evidence."
        )

        if dry_run:
            print(f"[DRY-RUN] Would inject candidate retest for claim {row['id']}: "
                  f"'{hypothesis[:60]}...' (wsc={row['weighted_support_count']:.1f})")
            injected += 1
            continue

        conn.execute("""
            INSERT INTO curiosities
            (text, priority, status, source_experiment, created_at, provenance)
            VALUES (?, 2, 'active', ?, ?, 'candidate_retest')
        """, (retest_text, row['first_experiment_id'], ts))
        injected += 1
        print(f"INJECTED candidate retest for claim {row['id']}: "
              f"'{hypothesis[:60]}...' (wsc={row['weighted_support_count']:.1f})")

    return injected



def inject_formal_replication_retests(conn, dry_run=False, quota=3):
    """Formal-replication supply for REPLICATED claims stuck at the
    ESTABLISHED gate with n_formal_replications = 0.

    A claim whose only retest ended 'disagreed_arbitrated' still counts an
    independent retest (so it holds REPLICATED) but has no PASSING formal
    replication — and inject_candidate_retests only serves CANDIDATEs, so
    nothing ever supplied the missing retest. Target the claims where this is
    a LIVE blocker (survived attack already in hand).

    Credit mechanics force one constraint: replication_results is
    UNIQUE(original_experiment_id) and the refiller moot-skips any retest
    whose source experiment already holds a credit row — so the curiosity's
    source_experiment MUST be a support experiment with NO existing
    replication_results row (n_formal counts rows on ANY support experiment
    via the claim_evidence join, so crediting a different support works).
    Claims where every support is already credited are skipped (logged) —
    they can only be unblocked by new evidence.

    Same [CANDIDATE-RETEST] lane, provenance, and active-only dedup as the
    CANDIDATE injector — every downstream rail (reserved lane, block 1g
    credit, reconciler, moot-skip) applies unchanged."""
    ts = time.time()
    rows = conn.execute("""
        SELECT kc.id, kc.hypothesis_text, kc.weighted_support_count,
               (SELECT ce.experiment_id FROM claim_evidence ce
                WHERE ce.claim_id = kc.id
                  AND COALESCE(ce.evidence_type, 'support') = 'support'
                  AND ce.experiment_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM replication_results rr
                                  WHERE rr.original_experiment_id = ce.experiment_id)
                ORDER BY ce.confidence DESC, ce.id DESC LIMIT 1) AS target_exp
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'REPLICATED'
          AND COALESCE(kc.is_meta, 0) = 0
          AND COALESCE(kc.n_formal_replications, 0) = 0
          AND EXISTS (SELECT 1 FROM adversarial_replications ar
                      WHERE ar.claim_id = kc.id AND ar.status = 'survived')
          AND NOT EXISTS (
              SELECT 1 FROM curiosities c
              JOIN claim_evidence ce2 ON ce2.experiment_id = c.source_experiment
              WHERE ce2.claim_id = kc.id
                AND c.provenance = 'candidate_retest'
                AND c.status = 'active')
        ORDER BY kc.weighted_support_count DESC
        LIMIT ?""", (quota,)).fetchall()

    injected = 0
    for row in rows:
        hypothesis = row['hypothesis_text']
        if not hypothesis or not row['target_exp']:
            if not row['target_exp']:
                print(f"  formal-replication retest for claim {row['id']}: every "
                      f"support already credited — needs new evidence, skipping")
            continue
        import re
        clean_hypothesis = re.sub(r'^\[TRANSFER from [^\]]+\]\s*', '', hypothesis)
        retest_text = (
            f"[CANDIDATE-RETEST] Replicate and validate: {clean_hypothesis[:200]}\n"
            f"This claim is REPLICATED (wsc={row['weighted_support_count']:.1f}) and "
            f"survived an adversarial attack, but has no PASSING formal replication "
            f"(its earlier retest was settled by arbitration). Run a controlled "
            f"replication: same hypothesis, different execution. If your experiment "
            f"supports the claim, report SUPPORTED. If it contradicts, report "
            f"REFUTED with evidence."
        )
        if dry_run:
            print(f"[DRY-RUN] Would inject formal-replication retest for claim "
                  f"{row['id']} (source {row['target_exp']}): '{hypothesis[:60]}...'")
            injected += 1
            continue
        conn.execute("""
            INSERT INTO curiosities
            (text, priority, status, source_experiment, created_at, provenance)
            VALUES (?, 2, 'active', ?, ?, 'candidate_retest')
        """, (retest_text, row['target_exp'], ts))
        injected += 1
        print(f"INJECTED formal-replication retest for claim {row['id']} "
              f"(source {row['target_exp']})")

    return injected


def cleanup_orphaned_evidence(conn, dry_run=False):
    """Delete claim_evidence rows that reference deleted or NULL worker_results.

    Worker_results are deleted over time (cleanup, rebuilds, corruption recovery)
    but the corresponding claim_evidence rows are not always cleaned up. These
    orphans inflate claim_evidence row counts and, historically, caused
    support_count to drift away from reality.

    Runs every 5 minutes as part of the contradiction_detector cron cycle,
    BEFORE recompute_weighted_support and demotion, so that wsc reflects only
    real evidence.

    Added June 19 2026 after finding 4,227 rows with NULL worker_result_id
    (never linked) plus the original 52,726 rows with non-NULL but deleted
    worker_result_id (cleaned by the one-shot fix_claim_lifecycle.py).
    """
    # NULL worker_result_id
    null_count = conn.execute(
        "SELECT COUNT(*) FROM claim_evidence WHERE worker_result_id IS NULL"
    ).fetchone()[0]

    # Non-NULL but references deleted worker_result
    dangling_count = conn.execute("""
        SELECT COUNT(*) FROM claim_evidence ce
        LEFT JOIN worker_results wr ON ce.worker_result_id = wr.id
        WHERE ce.worker_result_id IS NOT NULL AND wr.id IS NULL
    """).fetchone()[0]

    total = null_count + dangling_count
    if total == 0:
        return 0

    if dry_run:
        print(f"[DRY-RUN] Would delete {total} orphaned claim_evidence rows "
              f"({null_count} NULL, {dangling_count} dangling)")
        return total

    if null_count > 0:
        conn.execute("DELETE FROM claim_evidence WHERE worker_result_id IS NULL")
    if dangling_count > 0:
        conn.execute("""
            DELETE FROM claim_evidence
            WHERE worker_result_id NOT IN (SELECT id FROM worker_results)
              AND worker_result_id IS NOT NULL
        """)
    print(f"CLEANUP: deleted {total} orphaned claim_evidence rows "
          f"({null_count} NULL, {dangling_count} dangling)")
    return total


# Canonical artifact_status values and their rank for weighted_support_count
_CANONICAL_ARTIFACT_STATUS = {
    'VERIFIED', 'VERIFIED_PARTIAL', 'DEPLOYED', 'FILES_MISSING',
    'UNVERIFIED', 'FAILED', 'HISTORICAL_UNKNOWN',
}

_ARTIFACT_STATUS_NORMALIZATIONS = {
    'verified': 'VERIFIED', 'deployed': 'DEPLOYED',
    'PARTIAL': 'VERIFIED_PARTIAL', 'PASS': 'VERIFIED',
    'MISSING': 'FILES_MISSING', 'missing': 'FILES_MISSING',
    'completed': 'UNVERIFIED', 'NOT_VERIFIED': 'UNVERIFIED',
    '': 'UNVERIFIED',
    # 2026-07-12: stragglers found in audit. PASSED mirrors PASS (an assertion
    # the gate's checks passed); SUCCESS is a task-status leak like 'completed'
    # — the worker finished, nothing was gate-verified.
    'PASSED': 'VERIFIED', 'SUCCESS': 'UNVERIFIED',
}


def normalize_artifact_status(conn, dry_run=False):
    """Normalize non-canonical artifact_status values in worker_results.

    Various write paths (deploy scripts, harvest scripts, result_bridge)
    have historically written non-canonical values (lowercase variants,
    truncated forms, task statuses leaking in). This function normalizes
    them to the canonical set every 5 minutes as a safety net.

    Added June 19 2026 after finding 28 rows with non-canonical values.
    """
    fixed = 0
    for old_val, new_val in _ARTIFACT_STATUS_NORMALIZATIONS.items():
        if dry_run:
            count = conn.execute(
                "SELECT COUNT(*) FROM worker_results WHERE artifact_status = ?",
                (old_val,)
            ).fetchone()[0]
        else:
            cur = conn.execute(
                "UPDATE worker_results SET artifact_status = ? WHERE artifact_status = ?",
                (new_val, old_val)
            )
            count = cur.rowcount
        if count > 0:
            print(f"NORMALIZE: artifact_status {old_val!r} -> {new_val} ({count} rows)")
            fixed += count

    # Also fix NULL artifact_status (result_bridge doesn't set it)
    if dry_run:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM worker_results WHERE artifact_status IS NULL"
        ).fetchone()[0]
    else:
        cur = conn.execute(
            "UPDATE worker_results SET artifact_status = 'UNVERIFIED' WHERE artifact_status IS NULL"
        )
        null_count = cur.rowcount
    if null_count > 0:
        print(f"NORMALIZE: artifact_status NULL -> UNVERIFIED ({null_count} rows)")
        fixed += null_count

    return fixed


def run(dry_run=False):
    conn = get_db()

    # Commit after EACH pass below. This run used to be one long deferred
    # transaction (first write of the first pass -> final commit at the end),
    # holding the WAL write lock for the whole multi-minute cycle and starving
    # every concurrent writer ("database is locked" across the other cron
    # jobs). No pass needs cross-pass atomicity: each is idempotent and the
    # recomputes are single atomic UPDATE statements.

    # P3.5: cleanup orphaned evidence BEFORE recompute (so wsc reflects only real evidence)
    orphaned = cleanup_orphaned_evidence(conn, dry_run)
    conn.commit()

    # P3.6: normalize artifact_status so wsc weighting uses canonical values
    normalized = normalize_artifact_status(conn, dry_run)
    conn.commit()

    # P3: recompute weighted support before maturity recompute
    recompute_weighted_support(conn)
    conn.commit()

    # Path 1: replication disagreements -> DISPUTED
    # (contradiction_count is incremented here; the maturity recompute
    # reads it to determine DISPUTED status)
    r1 = detect_replication_contradictions(conn, dry_run)
    conn.commit()

    # Path 2: worker-level contradictions -> DISPUTED
    r2 = detect_worker_contradictions(conn, dry_run)
    conn.commit()

    # Spurious-agreement score: flag claims whose "supports" agree only on the
    # binary flag while disagreeing in substance (text/flag mismatch, direction,
    # or magnitude). Writes knowledge_claims.spurious_agreement (+ components) and
    # never touches posterior/claim_status itself. Computed BEFORE the maturity
    # recompute so the ESTABLISHED gate reads a fresh score (not last cycle's
    # stale value). Skipped on dry-run (read-only), in which case the gate falls
    # back to whatever score is already stored.
    sa_high = None
    if not dry_run:
        try:
            from spurious_agreement import compute_and_write
            sa_high = compute_and_write(conn, commit=True)
        except Exception as e:  # never break the lifecycle cycle
            sys.stderr.write(f"spurious_agreement hook failed: {e}\n")

    # Recompute retest/replication credit from replication_results before the
    # maturity recompute reads it. n_independent_retests was previously set
    # only by a one-shot migration — frozen after the June credit-writer loss;
    # intake now writes replication_results for candidate_retest completions,
    # so both counters must track the table each cycle.
    from maturity import (recompute_n_formal_replications,
                          recompute_n_independent_retests,
                          recompute_contradiction_count,
                          recompute_all_maturity)
    recompute_n_independent_retests(conn)
    conn.commit()
    recompute_n_formal_replications(conn)
    conn.commit()

    # Normalize contradiction_count to the live dispute basis (see
    # maturity.recompute_contradiction_count). Runs AFTER the detection
    # passes above (their inline increments are immediate marking; this is
    # the canonical value) and BEFORE the maturity recompute derives
    # DISPUTED from it. Without this the count was an increment-only
    # ratchet and DISPUTED a one-way door.
    if not dry_run:
        recompute_contradiction_count(conn)
    conn.commit()

    # Recompute maturity from provenance signals (replaces all promote/demote logic)
    mat_result = recompute_all_maturity(conn, dry_run=dry_run, verbose=False)

    # retest_gate lane RETIRED: a REPLICATED claim by definition already holds
    # its replication_results credit (that's what made it REPLICATED), and the
    # table is UNIQUE(original_experiment_id) — so a retest_gate completion
    # could never write anything. Disconfirmation pressure on REPLICATED
    # claims is the adversarial attack gate's job (adversarial_replication_
    # enqueuer.py), which supersedes this lane. inject_retest_curiosities()
    # is kept below, uncalled, for reference.
    retest_injected = 0

    # Candidate retest injection: drain the backlog of CANDIDATE claims with
    # wsc >= 2.0 that are blocked only by the retest gate. Priority 3, quota 5/cycle.
    candidate_retests = inject_candidate_retests(conn, dry_run)
    formal_retests = inject_formal_replication_retests(conn, dry_run)
    if formal_retests:
        print(f"Injected {formal_retests} formal-replication retest(s) "
              f"(REPLICATED, n_formal=0, survived attack)")

    if dry_run:
        print(f"\nDry-run summary: {orphaned} orphaned evidence rows, "
              f"{normalized} artifact_status normalized, "
              f"{mat_result['processed']} claims processed, "
              f"{mat_result['changes']} maturity transitions, "
              f"{r1} replication disputes, "
              f"{r2} worker contradictions, "
              f"{retest_injected} retest curiosities injected, "
              f"{candidate_retests} candidate retests injected")
        if mat_result["transitions"]:
            for old, new_map in sorted(mat_result["transitions"].items()):
                for new, count in sorted(new_map.items(), key=lambda x: -x[1]):
                    print(f"  {old} -> {new}: {count}")
    else:
        conn.commit()
        print(f"\nSummary: {orphaned} orphaned evidence rows, "
              f"{normalized} artifact_status normalized, "
              f"{mat_result['processed']} claims processed, "
              f"{mat_result['changes']} maturity transitions, "
              f"{r1} replication disputes, "
              f"{r2} worker contradictions, "
              f"{retest_injected} retest curiosities injected, "
              f"{candidate_retests} candidate retests injected, "
              f"{sa_high if sa_high is not None else 'n/a'} high-spurious-agreement claims")
        if mat_result["transitions"]:
            for old, new_map in sorted(mat_result["transitions"].items()):
                for new, count in sorted(new_map.items(), key=lambda x: -x[1]):
                    print(f"  {old} -> {new}: {count}")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Detect contradictory evidence in claim DB")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying")
    args = parser.parse_args()
    run(dry_run=args.dry_run)