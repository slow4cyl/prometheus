#!/usr/bin/env python3
"""claim_splitter.py — the exit path for spurious-support claims.

Why (2026-07-12): the SPURIOUS_SUPPORT shelf gate bins claims whose
spurious_agreement >= 0.6 — their "supporting" experiments carry one hypothesis
label but measure DIFFERENT operationalizations (#62492: 11 supports spanning
ratios 0.44-15.8 across ablation/gradient/architecture readings). UNRECONCILED
claims heal through arbitration; SA-binned claims had NO exit: heterogeneous
support never repairs itself. This lane is the constructive completion — an
external reviewer's "strict claim schema" proposal scoped to where it bites:

  * PROPOSER (LLM) reads the parent claim + its heterogeneous support findings
    and decomposes them into 2-4 DISTINCT sub-propositions, each with the full
    schema: IV, DV, dataset/generative process, intervention, metric, baseline,
    expected direction, regime.
  * SKEPTIC (LLM) independently verifies each sub-proposition is (a) a genuinely
    distinct operationalization, not a paraphrase, and (b) grounded in at least
    one of the shown findings. Reject-by-default on uncertainty.
  * Accepted sub-propositions enter the system through its ORGANIC intake:
    curiosities, with source_experiment = a real supporting experiment (so
    per-domain attribution flows) and provenance = 'claim_split:<parent_id>'.
    The normal pipeline scores them, tasks them, and their results form NEW
    precise claims via the ordinary hypothesis-hash path. The parent stays in
    its bin — display truth — while its question survives as testable pieces.

Ledger `claim_splits` (one row per parent, UNIQUE) makes the lane idempotent
and auditable. Same budget envelope as the other critics (the 300s cron kill
is real): proposals may not START inside the last VOTE_WORST seconds.

Usage:
    python3 claim_splitter.py [--dry-run] [--limit N] [--claim ID] [--verbose]
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db
from answer_consistency_adjudicator import call_llm

SA_MAX = 0.6              # mirrors maturity established_sa_max / routing.SPURIOUS_SA_MAX
MAX_FINDINGS = 12         # heterogeneous support findings shown to the proposer
FINDING_CHARS = 420
MAX_SUBCLAIMS = 4
CLAIM_CUTOFF = 100        # no NEW parent after this many seconds
RUN_DEADLINE = 240        # LLM calls must END by t0 + this
VOTE_WORST = 130          # worst-case wall time of one call_llm


def ensure_state(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claim_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_claim_id INTEGER NOT NULL UNIQUE,
            created_at REAL NOT NULL,
            status TEXT NOT NULL,          -- split | unsplittable | failed
            n_subclaims INTEGER,
            subclaims_json TEXT,
            curiosity_ids TEXT,
            skeptic_confidence REAL,
            reason TEXT
        )""")
    conn.commit()


def get_candidates(conn, limit, only_claim=None):
    """SA-binned shelf-band claims not yet split. Ledger-uniqueness makes the
    lane one-shot per parent — a failed/unsplittable verdict is re-attempted
    only via --claim (operator intent), never on cron churn."""
    where_claim = f"AND kc.id = {int(only_claim)}" if only_claim else ""
    return conn.execute(f"""
        SELECT kc.id, kc.hypothesis_text, kc.claim_summary, kc.claim_status,
               kc.domain, kc.spurious_agreement
        FROM knowledge_claims kc
        WHERE kc.spurious_agreement IS NOT NULL AND kc.spurious_agreement >= ?
          AND (kc.claim_status IN ('REPLICATED', 'ESTABLISHED')
               OR EXISTS(SELECT 1 FROM discovery_candidates dc
                         WHERE dc.claim_id = kc.id))
          AND COALESCE(kc.is_meta, 0) = 0
          AND NOT EXISTS(SELECT 1 FROM claim_splits s WHERE s.parent_claim_id = kc.id)
          {where_claim}
        ORDER BY kc.spurious_agreement DESC
        LIMIT ?""", (SA_MAX, limit)).fetchall()


def get_support(conn, claim_id):
    """The heterogeneous support: key findings + their experiment ids."""
    return conn.execute("""
        SELECT ce.experiment_id,
               COALESCE(NULLIF(TRIM(ce.key_finding), ''),
                        (SELECT wr.key_finding FROM worker_results wr
                         WHERE wr.id = ce.worker_result_id)) AS finding
        FROM claim_evidence ce
        WHERE ce.claim_id = ?
          AND COALESCE(ce.evidence_type, 'support') = 'support'
        ORDER BY ce.id DESC LIMIT ?""", (claim_id, MAX_FINDINGS)).fetchall()


SCHEMA_FIELDS = ("iv", "dv", "dataset_or_process", "intervention",
                 "metric", "baseline", "expected_direction", "regime")


def proposer_prompt(claim, findings):
    lines = "\n".join(f"- [{f['experiment_id']}] {(f['finding'] or '')[:FINDING_CHARS]}"
                      for f in findings if f["finding"])
    return f"""A research claim accumulated "supporting" experiments that DO NOT measure the same thing —
its false-consensus score is {claim['spurious_agreement']:.2f} (0.6+ = supports disagree on
operationalization). Your job: decompose it into the DISTINCT precise propositions its evidence
actually tested.

PARENT CLAIM (question): "{(claim['hypothesis_text'] or '')[:400]}"
PARENT SUMMARY: "{(claim['claim_summary'] or '')[:500]}"

ITS SUPPORT FINDINGS (heterogeneous):
{lines}

Identify 2-{MAX_SUBCLAIMS} genuinely DISTINCT operationalizations present in these findings — different
intervention, different dependent variable, different metric, or different regime. NOT paraphrases of
the parent. Each sub-proposition must be precise enough to preregister: one experiment could settle it.

STRICT JSON only:
{{"splittable": true|false,
  "reason": "one sentence — why these are distinct operationalizations (or why the claim is actually homogeneous)",
  "subclaims": [
    {{"hypothesis": "one testable sentence phrased as a question",
      "iv": "...", "dv": "...", "dataset_or_process": "...", "intervention": "...",
      "metric": "...", "baseline": "...", "expected_direction": "increase|decrease|null|conditional",
      "regime": "where it should hold", "grounded_in": ["exp_id", ...]}}
  ]}}"""


def skeptic_prompt(claim, findings, proposal):
    lines = "\n".join(f"- [{f['experiment_id']}] {(f['finding'] or '')[:FINDING_CHARS]}"
                      for f in findings if f["finding"])
    subs = json.dumps(proposal.get("subclaims", []), indent=1)[:3000]
    return f"""Verify a proposed decomposition of an ambiguous research claim. REJECT by default.

PARENT: "{(claim['hypothesis_text'] or '')[:300]}"
EVIDENCE:
{lines}

PROPOSED SUB-PROPOSITIONS:
{subs}

For the SET to pass, EVERY sub-proposition must be: (a) a genuinely DISTINCT operationalization
(different intervention/DV/metric/regime — not a rewording of the parent or of a sibling), and
(b) grounded in at least one shown finding (the grounded_in ids must actually discuss it).
If any sub-proposition fails, the set fails.

STRICT JSON only:
{{"accept": true|false, "confidence": 0.0-1.0,
  "reason": "one sentence citing the failing sub-proposition, or why the set is sound"}}"""


def parse_json_block(text):
    """Tolerant JSON extraction for a reasoning judge that wraps output in prose."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    for cand in (m.group(0), m.group(0).replace("\n", " ")):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def valid_subclaims(proposal):
    subs = proposal.get("subclaims") or []
    out = []
    for s in subs[:MAX_SUBCLAIMS]:
        if not isinstance(s, dict) or not (s.get("hypothesis") or "").strip():
            continue
        if sum(1 for k in SCHEMA_FIELDS if (s.get(k) or "").strip()) < 6:
            continue                       # schema too thin — not preregisterable
        out.append(s)
    return out


def curiosity_text(sub):
    """The precise sub-hypothesis, schema embedded so the eventual worker card
    carries the preregistration (IV/DV/metric/baseline/direction/regime)."""
    return (f"{sub['hypothesis'].strip()} "
            f"[SPLIT-SCHEMA IV={sub.get('iv','?')}; DV={sub.get('dv','?')}; "
            f"data={sub.get('dataset_or_process','?')}; intervention={sub.get('intervention','?')}; "
            f"metric={sub.get('metric','?')}; baseline={sub.get('baseline','?')}; "
            f"direction={sub.get('expected_direction','?')}; regime={sub.get('regime','?')}]")


def inject_curiosities(conn, parent_id, subs, findings, dry_run):
    """Sub-propositions enter through the organic intake: curiosities with a
    REAL supporting experiment as source (domain attribution flows) and
    provenance marking the split."""
    real_exp = next((f["experiment_id"] for f in findings if f["experiment_id"]), None)
    ids = []
    for sub in subs:
        text = curiosity_text(sub)[:900]
        if dry_run:
            ids.append(-1)
            continue
        cur = conn.execute(
            "INSERT INTO curiosities (text, priority, status, source_experiment, "
            "created_at, provenance) VALUES (?, 7, 'active', ?, ?, ?)",
            # [SPLIT] prefix = the lane key: sync_curiosity_views reserves a
            # queue slice for it, task_refiller's _LANE_TARGETS sub-pool mints
            # it, and prior_feed_stamp suppresses the RAG feed (sub-propositions
            # exist because supports DISAGREED — test them blind). Without the
            # prefix the subs sat mid-pack organic: 0/166 minted in 2 days.
            # normalize_hypothesis strips leading [tags], so claim identity is
            # the bare sub-proposition text, unaffected by the lane tag.
            (f"[SPLIT] {text}", real_exp, time.time(), f"claim_split:{parent_id}"))
        ids.append(cur.lastrowid)
    if not dry_run:
        conn.commit()
    return ids


def main():
    ap = argparse.ArgumentParser(
        description="Split spurious-support claims into precise sub-propositions")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=2)
    ap.add_argument("--claim", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    ensure_state(conn)
    if args.claim:
        conn.execute("DELETE FROM claim_splits WHERE parent_claim_id=? AND status!='split'",
                     (args.claim,))
        conn.commit()
    cands = get_candidates(conn, args.limit, only_claim=args.claim)
    if not cands:
        print("No spurious-support claims awaiting a split.")
        conn.close()
        return 0

    t0 = time.time()
    split = unsplittable = failed = 0
    for claim in cands:
        if time.time() - t0 > CLAIM_CUTOFF:
            print("  wall budget reached — deferring remaining claims")
            break
        findings = get_support(conn, claim["id"])
        if len([f for f in findings if f["finding"]]) < 3:
            continue        # too little text to decompose credibly

        def ask(prompt):
            if time.time() > t0 + RUN_DEADLINE - VOTE_WORST:
                return None
            return parse_json_block(call_llm(prompt, max_tokens=2400))

        proposal = ask(proposer_prompt(claim, findings))
        verdict, subs, skeptic = None, [], None
        if proposal is None:
            verdict = "failed"
        elif not proposal.get("splittable"):
            verdict = "unsplittable"
        else:
            subs = valid_subclaims(proposal)
            if len(subs) < 2:
                verdict = "unsplittable"
            else:
                skeptic = ask(skeptic_prompt(claim, findings, {"subclaims": subs}))
                if skeptic is None:
                    verdict = "failed"
                elif skeptic.get("accept") and float(skeptic.get("confidence") or 0) >= 0.7:
                    verdict = "split"
                else:
                    verdict = "unsplittable"

        cur_ids = []
        if verdict == "split":
            cur_ids = inject_curiosities(conn, claim["id"], subs, findings, args.dry_run)
            split += 1
            tag = "[DRY-RUN] would split" if args.dry_run else "SPLIT"
            print(f"{tag} claim {claim['id']} [{claim['claim_status']}, sa={claim['spurious_agreement']:.2f}] "
                  f"-> {len(subs)} sub-propositions (curiosities {cur_ids})")
            if args.verbose:
                for s in subs:
                    print(f"    - {s['hypothesis'][:110]}")
        elif verdict == "unsplittable":
            unsplittable += 1
            why = (proposal or {}).get("reason") or (skeptic or {}).get("reason") or "thin schema"
            print(f"  claim {claim['id']}: unsplittable — {str(why)[:140]}")
        else:
            failed += 1
            print(f"  claim {claim['id']}: split FAILED (no parseable proposal/verdict)")

        if not args.dry_run and verdict != "failed":
            # 'failed' = a transient LLM parse/timeout, NOT a judgment about the
            # claim — ledgering it would make one flaky call permanent (the
            # UNIQUE key blocks retries; observed 5 failed vs 5 split on day
            # one). Leave no row so the next cron tick retries naturally;
            # only real verdicts (split/unsplittable) are one-shot.
            conn.execute(
                "INSERT OR REPLACE INTO claim_splits (parent_claim_id, created_at, status, "
                "n_subclaims, subclaims_json, curiosity_ids, skeptic_confidence, reason) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (claim["id"], time.time(), verdict, len(subs),
                 json.dumps(subs)[:8000] if subs else None,
                 ",".join(str(i) for i in cur_ids) or None,
                 float((skeptic or {}).get("confidence") or 0) or None,
                 str((proposal or {}).get("reason") or "")[:400]))
            conn.commit()

    print(f"\nProcessed: {split} split, {unsplittable} unsplittable, {failed} failed")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
