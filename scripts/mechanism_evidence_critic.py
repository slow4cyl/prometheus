#!/usr/bin/env python3
"""mechanism_evidence_critic.py — test the WHY-IT-WORKS story against the
claim's own evidence (the explanation-vs-evidence axis).

Why (2026-07-12, mechanism-certainty assessment): mechanism prose earns ZERO
scoring credit anywhere (compute_maturity is numeric-only; discovery score has
no prose term) and prose-vs-CODE is already gated (method_code_mismatch cap) —
but nothing tests the causal STORY against the DATA. A worker can report a real
association and wrap it in a confident mechanism narrative the numbers never
tested: no mediator measured, no intervention on the proposed cause, alternative
stories fitting the same numbers equally well. An external reviewer's ask
("store the mechanism as a candidate explanation with separate tests") lands
exactly here.

Three verdicts, ONE gate:
  * MECHANISM_EVIDENCED — the shown evidence actually probes the story
    (mediator moves, intervention on the mechanism, alternatives excluded).
  * ASSOCIATION_ONLY — honest and common: the effect is real, the mechanism is
    a candidate explanation the evidence does not separately test. RECORDED,
    never gated — capping honest science for not testing its mechanism yet
    would flag half the shelf.
  * CONTRADICTED — the claim's own numbers cut AGAINST the stated mechanism
    (the mediator does not move, the story predicts the wrong sign somewhere,
    an internal control refutes it). This alone sets
    knowledge_claims.mechanism_unsupported = 1, which maturity caps at
    CANDIDATE beside circular_construction / method_code_mismatch /
    scope_conflict. Caps, never credits.

Majority vote (3, split escalates to 5), conflict at conf >= 0.7 with quoted
numbers, same 300s-proof budget envelope as its siblings. Fingerprint =
summary-hash + evidence ids: a rewrite or new evidence re-queues the claim —
never a resting tier.

Usage:
    python3 mechanism_evidence_critic.py [--dry-run] [--limit N] [--claim ID] [--verbose]
"""
import argparse
import hashlib
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db
from answer_consistency_adjudicator import call_llm

MAX_FINDINGS = 8
FINDING_CHARS = 450
HEADLINE_CHARS = 1200
FLAG_CONFIDENCE = 0.7
VOTES = 3
CLAIM_CUTOFF = 110
RUN_DEADLINE = 250
VOTE_WORST = 130

# a claim must actually TELL a mechanism story to be judged on one
_MECH_MARKERS = re.compile(
    r"\b(?:WHY IT WORKS|mechanism|because|driven by|explained by|due to|"
    r"mediat\w+|causal|arises from|stems from)\b", re.I)


def ensure_state(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mechanism_evidence_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            reviewed_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            verdict TEXT,                 -- MECHANISM_EVIDENCED | ASSOCIATION_ONLY | CONTRADICTED | NULL failed
            confidence REAL,
            reason TEXT,
            findings_seen INTEGER,
            model TEXT
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mer_claim_fp
        ON mechanism_evidence_reviews(claim_id, evidence_fingerprint)""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")]
    if "mechanism_unsupported" not in cols:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN mechanism_unsupported INTEGER")
    conn.commit()


def fingerprint(headline, evid):
    h = hashlib.md5((headline or "")[:HEADLINE_CHARS].encode()).hexdigest()[:12]
    ids = ",".join(str(e["experiment_id"]) for e in evid)
    return hashlib.md5(f"{h}|{ids}|{len(evid)}".encode()).hexdigest()


def get_candidates(conn, limit, only_claim=None, scan_budget_s=45):
    where_claim = f"AND kc.id = {int(only_claim)}" if only_claim else ""
    rows = conn.execute(f"""
        SELECT kc.id, kc.hypothesis_text, kc.claim_summary, kc.claim_status,
               COALESCE(kc.weighted_support_count, 0) AS wsc,
               EXISTS(SELECT 1 FROM discovery_candidates dc
                      WHERE dc.claim_id = kc.id) AS on_ledger
        FROM knowledge_claims kc
        WHERE (kc.claim_status IN ('REPLICATED', 'ESTABLISHED')
               OR EXISTS(SELECT 1 FROM discovery_candidates dc2
                         WHERE dc2.claim_id = kc.id))
          AND COALESCE(kc.is_meta, 0) = 0
          {where_claim}
        ORDER BY on_ledger DESC,
                 CASE kc.claim_status
                     WHEN 'ESTABLISHED' THEN 0
                     WHEN 'REPLICATED' THEN 1 ELSE 2 END,
                 kc.weighted_support_count DESC
    """).fetchall()

    seen = set(conn.execute(
        "SELECT claim_id, evidence_fingerprint FROM mechanism_evidence_reviews "
        "WHERE verdict IS NOT NULL"))
    deadline = time.time() + scan_budget_s
    out = []
    for row in rows:
        if time.time() > deadline or len(out) >= limit:
            break
        headline = (row["claim_summary"] or row["hypothesis_text"] or "")
        if not _MECH_MARKERS.search(headline):
            continue                      # no mechanism story told — nothing to judge
        evid = conn.execute("""
            SELECT ce.experiment_id,
                   COALESCE(NULLIF(TRIM(ce.key_finding), ''),
                            (SELECT wr.key_finding FROM worker_results wr
                             WHERE wr.id = ce.worker_result_id)) AS finding
            FROM claim_evidence ce
            WHERE ce.claim_id = ?
              AND COALESCE(ce.evidence_type, 'support') = 'support'
            ORDER BY ce.id DESC LIMIT ?""", (row["id"], MAX_FINDINGS)).fetchall()
        evid = [e for e in evid if e["finding"]]
        if len(evid) < 2:
            continue
        fp = fingerprint(headline, evid)
        if (row["id"], fp) in seen and not only_claim:
            continue
        out.append({"claim": row, "evid": evid, "fingerprint": fp})
    return out


def build_prompt(claim, evid):
    headline = (claim["claim_summary"] or claim["hypothesis_text"] or "")[:HEADLINE_CHARS]
    lines = "\n".join(f"- [{e['experiment_id']}] {(e['finding'] or '')[:FINDING_CHARS]}"
                      for e in evid)
    return f"""A research claim tells a causal MECHANISM story. Judge whether its own evidence actually
tests that story, merely shows the association, or cuts against it.

CLAIM (with its mechanism story):
"{headline}"

ITS SUPPORTING EVIDENCE (findings with numbers):
{lines}

Verdicts:
  MECHANISM_EVIDENCED — the evidence PROBES the story: a proposed mediator is measured and moves
    as claimed, the mechanism itself is intervened on, or a competing explanation is tested and
    excluded by the shown numbers.
  ASSOCIATION_ONLY — the effect/association is real in the numbers, but the mechanism paragraph
    is a candidate explanation the shown evidence never separately tests (no mediator measured,
    no intervention on the proposed cause, alternatives unexamined). This is COMMON and honest.
  CONTRADICTED — the claim's own numbers cut AGAINST the stated mechanism: the proposed mediator
    does not move, the story implies a sign/ordering the data reverses, or an internal control
    already refutes it.

Judge the MECHANISM story only — not whether the effect is real (other gates own that). Be
conservative: CONTRADICTED ONLY if you can QUOTE the numbers that cut against the story.
If the evidence is too thin to tell, ASSOCIATION_ONLY.

STRICT JSON only:
{{"verdict": "MECHANISM_EVIDENCED"|"ASSOCIATION_ONLY"|"CONTRADICTED", "confidence": 0.0-1.0,
  "reason": "one or two sentences; for CONTRADICTED quote the numbers that cut against the story"}}"""


def parse_review(text):
    if not text:
        return None
    vs = re.findall(r'"?verdict"?\s*:\s*"?(MECHANISM_EVIDENCED|ASSOCIATION_ONLY|CONTRADICTED)',
                    text, re.I)
    if not vs:
        return None
    cf = re.findall(r'"?confidence"?\s*:\s*([0-9.]+)', text)
    rs = re.search(r'"?reason"?\s*:\s*"([^"]{0,400})', text)
    try:
        conf = float(cf[-1]) if cf else 0.0
    except ValueError:
        conf = 0.0
    return {"verdict": vs[-1].upper(), "confidence": conf,
            "reason": rs.group(1) if rs else ""}


def judge(prompt, deadline=None):
    """3-vote majority; a split panel escalates to 5. CONTRADICTED needs a
    strict majority at conf >= FLAG_CONFIDENCE. Between the two non-gating
    verdicts, plurality wins (ties -> ASSOCIATION_ONLY, the humbler reading)."""
    verdicts = []
    target = VOTES
    attempts = 0
    while attempts < target + 3 and len(verdicts) < target:
        if deadline is not None and time.time() > deadline - VOTE_WORST:
            break
        attempts += 1
        v = parse_review(call_llm(prompt, max_tokens=1600))
        if v is not None:
            verdicts.append(v)
        if len(verdicts) == VOTES and target == VOTES:
            c = sum(1 for x in verdicts
                    if x["verdict"] == "CONTRADICTED" and x["confidence"] >= FLAG_CONFIDENCE)
            if 0 < c < len(verdicts):
                target = VOTES + 2
    if len(verdicts) < 2:
        return None
    contra = [v for v in verdicts
              if v["verdict"] == "CONTRADICTED" and v["confidence"] >= FLAG_CONFIDENCE]
    if len(contra) * 2 > len(verdicts):
        top = max(contra, key=lambda v: v["confidence"])
        return {"verdict": "CONTRADICTED",
                "confidence": round(sum(v["confidence"] for v in contra) / len(contra), 2),
                "reason": top["reason"], "votes": len(verdicts)}
    evidenced = [v for v in verdicts if v["verdict"] == "MECHANISM_EVIDENCED"]
    assoc = [v for v in verdicts if v["verdict"] == "ASSOCIATION_ONLY"]
    side = evidenced if len(evidenced) > len(assoc) else assoc
    if not side:
        side = verdicts
    top = max(side, key=lambda v: v["confidence"])
    return {"verdict": top["verdict"],
            "confidence": round(sum(v["confidence"] for v in side) / len(side), 2),
            "reason": top["reason"], "votes": len(verdicts)}


def main():
    ap = argparse.ArgumentParser(
        description="Judge WHY-IT-WORKS stories against the claim's own evidence")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--claim", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    ensure_state(conn)
    cands = get_candidates(conn, args.limit, only_claim=args.claim)
    if not cands:
        print("No mechanism-story claims awaiting review.")
        conn.close()
        return 0

    t0 = time.time()
    tallies = {"MECHANISM_EVIDENCED": 0, "ASSOCIATION_ONLY": 0, "CONTRADICTED": 0, "failed": 0}
    for c in cands:
        if time.time() - t0 > CLAIM_CUTOFF:
            print("  wall budget reached — deferring remaining claims")
            break
        claim = c["claim"]
        review = judge(build_prompt(claim, c["evid"]), deadline=t0 + RUN_DEADLINE)
        if review is None:
            tallies["failed"] += 1
            verdict = None
            print(f"  claim {claim['id']}: review FAILED (no quorum)")
        else:
            verdict = review["verdict"]
            tallies[verdict] += 1
            if verdict == "CONTRADICTED":
                tag = "[DRY-RUN] would flag" if args.dry_run else "FLAGGED"
                print(f"{tag} MECHANISM-CONTRADICTED claim {claim['id']} "
                      f"[{claim['claim_status']}] (conf={review['confidence']:.2f}, "
                      f"votes={review['votes']}): {review['reason'][:220]}")
            elif args.verbose:
                print(f"  claim {claim['id']}: {verdict} "
                      f"(conf={review['confidence']:.2f}, votes={review['votes']}) "
                      f"{review['reason'][:110]}")
        if not args.dry_run:
            if verdict is not None:
                conn.execute(
                    "UPDATE knowledge_claims SET mechanism_unsupported = ? WHERE id = ?",
                    (1 if verdict == "CONTRADICTED" else 0, claim["id"]))
            conn.execute("""
                INSERT INTO mechanism_evidence_reviews
                (claim_id, reviewed_at, evidence_fingerprint, verdict, confidence,
                 reason, findings_seen, model)
                VALUES (?,?,?,?,?,?,?,?)""",
                (claim["id"], time.time(), c["fingerprint"], verdict,
                 review["confidence"] if review else None,
                 (review.get("reason") or "")[:500] if review else "review failed",
                 len(c["evid"]), "deepseek/deepseek-v4-flash"))
            conn.commit()

    print(f"\nReviewed: {tallies}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
