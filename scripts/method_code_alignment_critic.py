#!/usr/bin/env python3
"""method_code_alignment_critic.py — flag claims whose described METHOD does not
match the experiment CODE that actually ran (ScientistOne CoE audit, check I4).

Why (2026-07-07, field gap analysis): the drift flag (central_quantity_drift)
checks whether a claim's NUMBERS agree across finding/residue/scope, and
circularity_critic checks whether the construction is self-fulfilling — but
NOTHING checks whether the claim's PROSE method faithfully describes the code.
A claim can report real, reproducible numbers while the code implements something
entirely different from what the finding says (ScientistOne's canonical example:
prose claims "bitwise integer encoding / O(1) surrogate cost model", the code runs
standard Python sets + a full simulator). That mismatch makes the method
irreproducible regardless of score accuracy — a self-deception the number-checks
cannot see.

This critic reads the claim's method description and the preserved experiment code
side-by-side and asks: does the code IMPLEMENT the described method? Acceptable
simplification (omitting low-level detail) is aligned; only a FUNDAMENTAL
algorithmic divergence is flagged. Conservative: flags only with quoted evidence
and critic-confidence >= 0.7.

Evidence: archived code from ~/.hermes/artifacts/<task_id|exp_id>/ (preserved at
result-write time — artifact_preserve.py). Claims with NO preserved code are
SKIPPED — there is nothing to align against.

CALIBRATION-FIRST: writes a method_code_reviews audit row + a
knowledge_claims.method_code_mismatch column, but maturity.py does NOT read that
column yet — this is report-only until the hit rate is validated on a real sample.
--dry-run writes nothing at all.

Usage:
    python3 method_code_alignment_critic.py [--dry-run] [--limit N] [--claim ID] [--verbose]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db
from answer_consistency_adjudicator import call_llm      # same OpenRouter plumbing
from circularity_critic import find_archived_code        # reuse artifact lookup

MAX_EXPERIMENTS = 3       # supporting experiments examined per claim
FLAG_CONFIDENCE = 0.7     # critic confidence required to flag
HEAD_CHARS = 5200         # top of file (imports + method/defs)
TAIL_CHARS = 4200         # bottom of file (the __main__ / execution block)


def excerpt_code(path):
    """Head + tail of a code file. The top carries the method/definitions, the
    BOTTOM carries the __main__/execution block — a top-only truncation hides the
    run and makes the judge cry 'stub code' (calibration false positives on
    #57544/#57942, whose real 10-28KB files execute below a 6KB cut)."""
    try:
        with open(path, errors="replace") as f:
            code = f.read(60000)
    except OSError:
        return None
    if len(code) <= HEAD_CHARS + TAIL_CHARS + 200:
        return code
    return (code[:HEAD_CHARS] + "\n\n# ... [middle of file elided] ...\n\n"
            + code[-TAIL_CHARS:])


def find_code(hermes, exp_id, kanban_task_id):
    """circularity_critic.find_archived_code locates the file; we re-read it as a
    head+tail excerpt so the execution block is never hidden from the judge."""
    path, _ = find_archived_code(hermes, exp_id, kanban_task_id)
    return (path, excerpt_code(path)) if path else (None, None)


def ensure_state(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS method_code_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            reviewed_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            mismatch INTEGER,             -- 1 flagged / 0 aligned / NULL failed
            confidence REAL,
            reason TEXT,
            code_seen INTEGER,
            model TEXT
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_mca_claim_fp
        ON method_code_reviews(claim_id, evidence_fingerprint)""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")]
    if "method_code_mismatch" not in cols:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN method_code_mismatch INTEGER")
    conn.commit()


def get_candidates(conn, limit, only_claim=None, scan_budget_s=240):
    """scan_budget_s (2026-07-09): the scan walks candidates (evidence query +
    artifact find_code per claim) until it collects `limit` unreviewed claims
    WITH preserved code. As the reviewed set grows the scan digs deeper each
    run — at ~650 reviews it crossed the 300s cron timeout (progressive
    slowdown by design). Budgeted scan: stop gracefully at the deadline and
    review what was collected; the next run continues deeper. Semantics
    otherwise identical."""
    import time as _time
    _deadline = _time.time() + scan_budget_s
    where_claim = f"AND kc.id = {int(only_claim)}" if only_claim else ""
    rows = conn.execute(f"""
        SELECT kc.id, kc.hypothesis_text, kc.claim_summary, kc.claim_status,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM knowledge_claims kc
        WHERE (kc.claim_status IN ('REPLICATED', 'ESTABLISHED')
               OR (kc.claim_status = 'CANDIDATE'
                   AND COALESCE(kc.weighted_support_count, 0) >= 2.0))
          AND kc.method_code_mismatch IS NULL
          AND COALESCE(kc.is_meta, 0) = 0
          {where_claim}
        ORDER BY CASE kc.claim_status
                     WHEN 'ESTABLISHED' THEN 0
                     WHEN 'REPLICATED' THEN 1 ELSE 2 END,
                 kc.weighted_support_count DESC
    """).fetchall()

    hermes = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # One query instead of a per-claim SELECT: the full reviewed set.
    seen_pairs = set(conn.execute(
        "SELECT claim_id, evidence_fingerprint FROM method_code_reviews "
        "WHERE mismatch IS NOT NULL"))
    out = []
    truncated = False
    for row in rows:
        if _time.time() > _deadline:
            truncated = True
            break
        evid = conn.execute("""
            SELECT ce.experiment_id,
                   COALESCE(NULLIF(TRIM(ce.key_finding), ''), e.result) AS finding,
                   e.kanban_task_id
            FROM claim_evidence ce
            LEFT JOIN experiments e ON e.id = ce.experiment_id
            WHERE ce.claim_id = ?
              AND COALESCE(ce.evidence_type, 'support') = 'support'
            ORDER BY ce.id DESC
        """, (row["id"],)).fetchall()
        exps = []
        for r in evid:
            path, code = find_code(hermes, r["experiment_id"], r["kanban_task_id"])
            if code and len(code.strip()) > 200:   # need real code to align against
                exps.append({"experiment_id": r["experiment_id"],
                             "finding": str(r["finding"] or "")[:900],
                             "code_path": path, "code": code})
            if len(exps) >= MAX_EXPERIMENTS:
                break
        if not exps:                                # no preserved code → check is moot
            continue
        fp = hashlib.md5(("|".join(sorted(e["experiment_id"] or "" for e in exps))
                          + f"#{len(exps)}").encode()).hexdigest()
        if (row["id"], fp) in seen_pairs and not only_claim:
            continue
        out.append({"claim": row, "exps": exps, "fingerprint": fp})
        if len(out) >= limit:
            break
    if truncated:
        print(f"  [scan budget hit after {scan_budget_s}s — reviewing "
              f"{len(out)} collected candidates; next run continues deeper]")
    return out


def build_prompt(method_text, exps):
    parts = []
    for e in exps:
        parts.append(
            f"[{e['experiment_id']}]\nFINDING: {e['finding']}\n"
            f"CODE ({os.path.basename(e['code_path'])}):\n```python\n{e['code']}\n```")
    material = "\n\n".join(parts)
    return f"""CLAIM's stated method / finding:
"{method_text}"

Supporting experiment(s) — the finding text plus the code that actually ran:

{material}

QUESTION — for EACH experiment shown, does its CODE implement what its OWN FINDING describes? Flag a
MISMATCH only for a FUNDAMENTAL algorithmic divergence between a shown finding and ITS OWN shown code,
for example:
  - the finding describes a sophisticated method ("bitwise integer encoding", "O(1) surrogate cost
    model", "spectral clustering") but that experiment's code runs something trivially different (a
    plain set, a full brute-force simulator, a random baseline);
  - the finding claims a mechanism / estimator / transform the code never applies;
  - the reported quantity is computed on different data or a different operation than described.

CRUCIAL — judge each experiment's code against ITS OWN finding (shown together in the same block). Do
NOT flag a mismatch merely because the CLAIM SUMMARY at the top mentions a method, regime, or dataset
that is absent from the code below — a claim usually draws on OTHER supporting experiments not shown
here, so a method missing from these snippets is NOT a mismatch. Only a contradiction between a shown
finding and its own shown code counts.

NOT a mismatch: acceptable simplification (the finding omits low-level detail the code handles);
mathematically equivalent formulations; a faithful-but-terser implementation. Judge fidelity of the
METHOD, not whether the numbers are right.

Be conservative: flag mismatch=true ONLY if you can point to the divergence — quote the finding AND
the conflicting code, and name the experiment. If the code is too partial to tell, mismatch=false.
Keep "reason" to ONE short sentence.

STRICT JSON only:
{{"mismatch": true|false, "confidence": 0.0-1.0,
  "reason": "one or two sentences quoting the described method and the conflicting code (or why aligned)",
  "experiments_implicated": ["exp_id", ...]}}"""


def parse_review(text):
    """Field-extraction parse (robust to a reasoning judge that wraps the JSON in
    prose, truncates the tail, or drops the braces): pull the LAST mismatch verdict
    + confidence directly. The prompt emits mismatch FIRST, so even a truncated
    reply carries it; taking the LAST occurrence skips any hypothetical the model
    floats mid-reasoning."""
    if not text:
        return None
    mms = re.findall(r'"?mismatch"?\s*:\s*(true|false)', text, re.I)
    if not mms:
        return None
    cf = re.findall(r'"?confidence"?\s*:\s*([0-9.]+)', text)
    rs = re.search(r'"?reason"?\s*:\s*"([^"]{0,400})', text)
    try:
        conf = float(cf[-1]) if cf else 0.0
    except ValueError:
        conf = 0.0
    return {"mismatch": mms[-1].lower() == "true", "confidence": conf,
            "reason": rs.group(1) if rs else ""}


VOTES = 3   # majority vote — the reasoning judge is non-deterministic (flip-flops
            # + intermittent unparseable output); one call can't be trusted to gate.


def judge(prompt, deadline=None):
    """Majority vote over VOTES independent judge calls. Retries absorb the parse
    failures; a MISMATCH requires a strict majority of the parseable votes to say
    mismatch at conf >= FLAG_CONFIDENCE — one bad run can never cap a claim. Returns
    {mismatch, confidence, reason, votes} or None if a quorum (>=2) never parsed.

    deadline (2026-07-09): hard wall-clock cutoff. One judge() could run
    5 votes x 60s-timeout calls (~325s) and blow the 300s cron budget solo
    when the endpoint is slow (observed: 13s for a trivial call). Voting
    stops at the deadline; quorum logic unchanged (>=2 parseable or None)."""
    verdicts = []
    for _ in range(VOTES + 2):              # a couple of extra tries to reach quorum
        if len(verdicts) >= VOTES:
            break
        if deadline is not None and time.time() > deadline:
            break
        v = parse_review(call_llm(prompt, max_tokens=1600))
        if v is not None:
            verdicts.append(v)
    if len(verdicts) < 2:
        return None
    mm = [v for v in verdicts if v["mismatch"] and v["confidence"] >= FLAG_CONFIDENCE]
    is_mm = len(mm) * 2 > len(verdicts)     # strict majority say method != code
    side = mm if is_mm else [v for v in verdicts if v not in mm]
    conf = sum(v["confidence"] for v in side) / max(1, len(side))
    reason = max(side, key=lambda v: v["confidence"])["reason"] if side else ""
    return {"mismatch": is_mm, "confidence": round(conf, 2),
            "reason": reason, "votes": len(verdicts)}


def main():
    ap = argparse.ArgumentParser(
        description="Flag claims whose described method != the code that ran (CoE audit I4)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--claim", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    ensure_state(conn)
    cands = get_candidates(conn, args.limit, only_claim=args.claim)
    if not cands:
        print("No claims need method-code review (none in band with preserved code).")
        conn.close()
        return 0

    flagged = aligned = failed = 0
    t0 = time.time()
    for c in cands:
        # Start-cutoff 150s + a hard 270s deadline threaded into judge(): a
        # claim started late gets a truncated vote (quorum >=2 still applies)
        # instead of overrunning the 300s cron kill (2026-07-09).
        if time.time() - t0 > 150:
            print("  wall budget reached — deferring remaining claims")
            break
        claim = c["claim"]
        method_text = (claim["claim_summary"] or claim["hypothesis_text"] or "")[:1400]
        review = judge(build_prompt(method_text, c["exps"]), deadline=t0 + 270)
        code_seen = len(c["exps"])
        if review is None:
            failed += 1
            verdict = None
            print(f"  claim {claim['id']}: review FAILED (no quorum of {VOTES} votes parsed)")
        else:
            is_mm = review["mismatch"]      # already a strict majority at conf >= FLAG_CONFIDENCE
            verdict = 1 if is_mm else 0
            if is_mm:
                flagged += 1
                tag = "[DRY-RUN] would flag" if args.dry_run else "FLAGGED"
                print(f"{tag} METHOD!=CODE claim {claim['id']} [{claim['claim_status']}] "
                      f"(conf={review['confidence']:.2f}, votes={review['votes']}, code_seen={code_seen}): "
                      f"{review['reason'][:240]}")
            else:
                aligned += 1
                if args.verbose:
                    print(f"  claim {claim['id']}: aligned "
                          f"(conf={review['confidence']:.2f}, votes={review['votes']}, code_seen={code_seen}) "
                          f"{review['reason'][:120]}")
        if not args.dry_run:
            if verdict is not None:
                conn.execute("UPDATE knowledge_claims SET method_code_mismatch = ? WHERE id = ?",
                             (verdict, claim["id"]))
            conn.execute("""
                INSERT INTO method_code_reviews
                (claim_id, reviewed_at, evidence_fingerprint, mismatch,
                 confidence, reason, code_seen, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (claim["id"], time.time(), c["fingerprint"], verdict,
                 review["confidence"] if review else None,
                 (review.get("reason") or "")[:500] if review else "review failed",
                 code_seen, "deepseek/deepseek-v4-flash"))
            conn.commit()

    print(f"\nReviewed {len(cands)} claim(s): {aligned} aligned, {flagged} method!=code, {failed} failed")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
