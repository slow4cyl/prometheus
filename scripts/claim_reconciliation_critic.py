#!/usr/bin/env python3
"""claim_reconciliation_critic.py — flag claims whose HEADLINE and MAPPED SCOPE
assert different propositions (the claim-reconciliation gate).

Why (2026-07-12, external review of the discovery page): the pipeline is built to
CREATE disagreement — arbitration writes the headline (claim_summary), the attack
lane writes MAPPED SCOPE cards (claim_scopes) — but nothing ever checks that the
two assert the SAME canonical proposition before the shelf renders them together.
Verified conflicts sitting in `hardening`: #64355 headline "7 unique optimal LRs
spanning 7.0x" vs its own scope "ALL ranks share optimal LR=0.1 … no differential
optimal LR exists"; #63438 summary "SVDD cross>within p=0.025" vs scope "SVDD
minimal effect; degradation mechanism REFUTED"; #65238 a text-classification
inversion and an OLS overfitting inversion attached to one claim. A discovery
record needs ONE claim, ONE decisive variable, ONE direction, ONE regime — until
reconciled, the entry is a research-debate ledger item, not a discovery.

This critic reads the headline and the scope cards side-by-side and asks: same
proposition? A scope that NARROWS the claim (adds boundary conditions, refines a
threshold, reports regime statistics) is the attack lane doing its job —
RECONCILED. Only a REVERSAL of the central conclusion (direction flipped, the
decisive variable displaced, the effect negated) or an IDENTITY mismatch (the
scope tests a different operationalization/question) is a conflict. Conservative:
majority vote at critic-confidence >= 0.7, with quoted evidence.

Writes knowledge_claims.scope_conflict (1 conflict / 0 reconciled / NULL
unreviewed) + a claim_reconciliation_reviews audit row. Consumers of the flag:
  * maturity.py caps scope_conflict=1 at CANDIDATE (mirrors method_code_mismatch);
  * discovery_spotlight / discovery_report route flagged claims OFF the shelf into
    a visible "unreconciled" bin (discovery_routing.UNRECONCILED).
On each conflict the critic enqueues a RECONCILIATION arbitration task (same
--claim plumbing as discovery hardening, capped per run). The loop then closes by
itself: the arbitration result attaches as evidence -> refresh_claim_summary
re-picks the headline -> the review fingerprint changes -> this critic re-reviews
-> the flag clears if the texts now agree. Unlike its template, candidates are
NOT filtered on `scope_conflict IS NULL` — a fingerprint change (summary rewrite
or new scope) re-queues the claim, so flagged claims are never a resting tier.

Usage:
    python3 claim_reconciliation_critic.py [--dry-run] [--limit N] [--claim ID]
                                           [--max-enqueue N] [--verbose]
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db
from answer_consistency_adjudicator import call_llm      # same OpenRouter plumbing

MAX_SCOPES = 3            # newest scope cards shown to the judge per claim
SCOPE_CHARS = 900         # per scope card
HEADLINE_CHARS = 1400
FLAG_CONFIDENCE = 0.7     # critic confidence required for a conflict vote to count
VOTES = 3                 # majority vote — single reasoning-judge calls flip-flop
ENQUEUER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "adversarial_replication_enqueuer.py")


def ensure_state(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claim_reconciliation_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            reviewed_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            conflict INTEGER,             -- 1 conflict / 0 reconciled / NULL failed
            conflict_type TEXT,           -- CONTRADICTION | IDENTITY_MISMATCH | NULL
            confidence REAL,
            reason TEXT,
            scopes_seen INTEGER,
            enqueued_task TEXT,
            model TEXT
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_crr_claim_fp
        ON claim_reconciliation_reviews(claim_id, evidence_fingerprint)""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")]
    if "scope_conflict" not in cols:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN scope_conflict INTEGER")
    conn.commit()


def fingerprint(headline, scope_rows):
    """Changes when the headline is rewritten OR a new scope card lands — either
    one re-queues the claim for review. This is the self-healing hook: a
    reconciliation rewrite reaches the summary via refresh_claim_summary, the
    fingerprint moves, and the next pass can clear the flag."""
    h = hashlib.md5((headline or "")[:HEADLINE_CHARS].encode()).hexdigest()[:12]
    ids = ",".join(str(r["id"]) for r in scope_rows)
    return hashlib.md5(f"{h}|{ids}|{len(scope_rows)}".encode()).hexdigest()


def get_candidates(conn, limit, only_claim=None, scan_budget_s=60):
    """Shelf-band claims with at least one mapped scope: everything on the
    discovery ledger first (the public face), then REPLICATED/ESTABLISHED by
    support depth. The seen-set is (claim_id, fingerprint) — NOT a scope_conflict
    IS NULL filter — so reviewed claims re-enter whenever their headline or scope
    set changes (never a resting tier)."""
    deadline = time.time() + scan_budget_s
    where_claim = f"AND kc.id = {int(only_claim)}" if only_claim else ""
    rows = conn.execute(f"""
        SELECT kc.id, kc.hypothesis_text, kc.claim_summary, kc.claim_status,
               COALESCE(kc.weighted_support_count, 0) AS wsc,
               EXISTS(SELECT 1 FROM discovery_candidates dc
                      WHERE dc.claim_id = kc.id) AS on_ledger
        FROM knowledge_claims kc
        WHERE EXISTS(SELECT 1 FROM claim_scopes cs WHERE cs.claim_id = kc.id)
          AND (kc.claim_status IN ('REPLICATED', 'ESTABLISHED')
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
        "SELECT claim_id, evidence_fingerprint FROM claim_reconciliation_reviews "
        "WHERE conflict IS NOT NULL"))
    out = []
    for row in rows:
        if time.time() > deadline:
            print(f"  [scan budget hit — reviewing {len(out)} collected candidates]")
            break
        scopes = conn.execute(
            "SELECT id, scope_text FROM claim_scopes WHERE claim_id = ? "
            "ORDER BY id DESC LIMIT ?", (row["id"], MAX_SCOPES)).fetchall()
        if not scopes:
            continue
        headline = (row["claim_summary"] or row["hypothesis_text"] or "")
        fp = fingerprint(headline, scopes)
        if (row["id"], fp) in seen and not only_claim:
            continue
        out.append({"claim": row, "scopes": scopes, "fingerprint": fp})
        if len(out) >= limit:
            break
    return out


def build_prompt(claim, scopes):
    headline = (claim["claim_summary"] or claim["hypothesis_text"] or "")[:HEADLINE_CHARS]
    question = (claim["hypothesis_text"] or "")[:300]
    cards = "\n\n".join(
        f"[scope card {i + 1}, newest first]\n{(s['scope_text'] or '')[:SCOPE_CHARS]}"
        for i, s in enumerate(scopes))
    return f"""A research claim is displayed with a HEADLINE (its arbitrated central finding) and
MAPPED SCOPE cards (what adversarial attacks found about where it holds). Decide whether they
assert the SAME canonical proposition.

ORIGINAL QUESTION (the claim's identity):
"{question}"

HEADLINE:
"{headline}"

MAPPED SCOPE:
{cards}

Verdicts:
  RECONCILED — the scope CONFIRMS or NARROWS the headline: adds boundary conditions ("holds only
    for N>=1000"), refines a threshold (~1.0 -> ~0.75) while the core mechanism holds, reports
    regime-specific statistics, or maps where the effect fails while the headline's central
    conclusion stands inside the mapped regime. Narrowing is the attack lane doing its job — it
    is NOT a conflict.
  CONTRADICTION — the scope REVERSES or NEGATES the headline's central conclusion: the effect
    direction flips; the scope says the headline's decisive variable has minimal/no effect and a
    DIFFERENT variable drives the result; the scope explicitly negates the headline's central
    quantity ("no differential X exists", "mechanism REFUTED", "not significantly different")
    while the headline asserts it.
  IDENTITY_MISMATCH — the scope tests a DIFFERENT proposition than the headline: a different
    dependent variable, a different operationalization of the same words, or evidence that
    plainly belongs to another experiment's question.

Judge the CENTRAL conclusion only — the finding the headline exists to report. Differences in
side numbers, sample sizes, or auxiliary observations do not count. Judge PROPOSITIONS, not
narration tone: a scope card may frame itself as a refinement ("the deeper truth is …",
"confirming …") while still negating the headline's central quantity — if the headline asserts
N distinct optima and the scope says all optima are identical, that is a CONTRADICTION however
it is framed. Be conservative: verdict CONTRADICTION or IDENTITY_MISMATCH ONLY if you can QUOTE
the headline assertion and the scope text that reverses/negates/mismatches it. If the scope is
too thin to tell, RECONCILED.

STRICT JSON only:
{{"verdict": "RECONCILED"|"CONTRADICTION"|"IDENTITY_MISMATCH", "confidence": 0.0-1.0,
  "axis": "direction|decisive_variable|negation|regime|identity|none",
  "reason": "one or two sentences quoting the headline claim and the conflicting scope text (or why reconciled)"}}"""


def parse_review(text):
    """Field-extraction parse (the judge wraps JSON in prose / truncates tails):
    the LAST verdict occurrence wins so mid-reasoning hypotheticals are skipped;
    the prompt emits verdict first, so even a truncated reply carries it."""
    if not text:
        return None
    vs = re.findall(r'"?verdict"?\s*:\s*"?(RECONCILED|CONTRADICTION|IDENTITY_MISMATCH)',
                    text, re.I)
    if not vs:
        return None
    cf = re.findall(r'"?confidence"?\s*:\s*([0-9.]+)', text)
    ax = re.findall(r'"?axis"?\s*:\s*"([a-z_]+)"', text, re.I)
    rs = re.search(r'"?reason"?\s*:\s*"([^"]{0,400})', text)
    try:
        conf = float(cf[-1]) if cf else 0.0
    except ValueError:
        conf = 0.0
    return {"verdict": vs[-1].upper(), "confidence": conf,
            "axis": (ax[-1].lower() if ax else ""),
            "reason": rs.group(1) if rs else ""}


def judge(prompt, deadline=None):
    """Majority vote over VOTES independent calls. A CONFLICT requires a strict
    majority of parseable votes saying CONTRADICTION/IDENTITY_MISMATCH at
    conf >= FLAG_CONFIDENCE — one bad run can never pull a claim off the shelf.
    A SPLIT 3-panel escalates once to 5 votes: boundary claims flip-flopped
    across whole runs under majority-of-3 (#64355 went 3-0 conflict then 1-2
    reconciled on the same prompt) — with escalation, a single boundary vote
    can never decide a claim. Returns {conflict, conflict_type, confidence,
    reason, votes} or None if a quorum (>=2) never parsed."""
    verdicts = []
    target = VOTES
    attempts = 0
    while attempts < target + 3 and len(verdicts) < target:
        if deadline is not None and time.time() > deadline:
            break
        attempts += 1
        v = parse_review(call_llm(prompt, max_tokens=1600))
        if v is not None:
            verdicts.append(v)
        if len(verdicts) == VOTES and target == VOTES:
            c = sum(1 for x in verdicts
                    if x["verdict"] != "RECONCILED" and x["confidence"] >= FLAG_CONFIDENCE)
            if 0 < c < len(verdicts):        # split panel → grow to 5
                target = VOTES + 2
    if len(verdicts) < 2:
        return None
    conflict_votes = [v for v in verdicts
                      if v["verdict"] != "RECONCILED" and v["confidence"] >= FLAG_CONFIDENCE]
    is_conflict = len(conflict_votes) * 2 > len(verdicts)
    side = conflict_votes if is_conflict else [v for v in verdicts if v not in conflict_votes]
    conf = sum(v["confidence"] for v in side) / max(1, len(side))
    top = max(side, key=lambda v: v["confidence"]) if side else {}
    ctype = None
    if is_conflict:
        ids = [v["verdict"] for v in conflict_votes]
        ctype = max(set(ids), key=ids.count)
    return {"conflict": is_conflict, "conflict_type": ctype,
            "confidence": round(conf, 2), "reason": top.get("reason", ""),
            "axis": top.get("axis", ""), "votes": len(verdicts)}


def build_note(claim_id, headline, scope_text, reason):
    return (
        f"CLAIM RECONCILIATION. Claim #{claim_id} is displayed with a headline and a mapped "
        f"scope that assert DIFFERENT propositions — a cross-family judge panel found: {reason}\n"
        f"HEADLINE: {headline[:500]}\n"
        f"MAPPED SCOPE: {scope_text[:500]}\n"
        f"Your job is NOT a new attack. ADJUDICATE which proposition the evidence actually "
        f"supports — re-run the decisive comparison if needed — and state the ONE canonical "
        f"claim: its decisive variable, its effect direction, and the regime where it holds. "
        f"Explicitly name which of the two texts is superseded and why. If neither survives, "
        f"say so plainly. Report ARBITRATION_VERDICT: <the reconciled canonical claim>."
    )


def enqueue_reconciliation(claim_id, note):
    """Same --claim plumbing discovery hardening uses; the enqueuer dedups against
    a live attack on the claim. Returns (task_id_or_None, already_live)."""
    res = subprocess.run([sys.executable, ENQUEUER, "--claim", str(claim_id),
                          "--note", note],
                         capture_output=True, text=True, timeout=180)
    task = re.search(r"t_[a-f0-9]+", res.stdout or "")
    live = "already has a live attack" in (res.stdout or "")
    ok = res.returncode == 0 and not live
    return (task.group(0) if (task and ok) else None), live


def main():
    ap = argparse.ArgumentParser(
        description="Flag claims whose headline and mapped scope assert different propositions")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--claim", type=int, default=None)
    ap.add_argument("--max-enqueue", type=int, default=2,
                    help="reconciliation arbitration tasks enqueued per run")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    ensure_state(conn)
    cands = get_candidates(conn, args.limit, only_claim=args.claim)
    if not cands:
        print("No claims need reconciliation review (band+scoped set unchanged).")
        conn.close()
        return 0

    flagged = reconciled = failed = enqueued = 0
    t0 = time.time()
    for c in cands:
        # same wall budgets as the method-code critic: no new claim after 150s,
        # votes stop at 270s — a slow judge never overruns the cron kill.
        if time.time() - t0 > 150:
            print("  wall budget reached — deferring remaining claims")
            break
        claim = c["claim"]
        review = judge(build_prompt(claim, c["scopes"]), deadline=t0 + 270)
        task_id = None
        if review is None:
            failed += 1
            verdict = None
            print(f"  claim {claim['id']}: review FAILED (no quorum of {VOTES} votes parsed)")
        elif review["conflict"]:
            flagged += 1
            verdict = 1
            tag = "[DRY-RUN] would flag" if args.dry_run else "FLAGGED"
            print(f"{tag} UNRECONCILED claim {claim['id']} [{claim['claim_status']}] "
                  f"{review['conflict_type']}/{review['axis']} "
                  f"(conf={review['confidence']:.2f}, votes={review['votes']}): "
                  f"{review['reason'][:240]}")
            if not args.dry_run and enqueued < args.max_enqueue:
                headline = (claim["claim_summary"] or claim["hypothesis_text"] or "")
                note = build_note(claim["id"], headline,
                                  c["scopes"][0]["scope_text"] or "", review["reason"])
                task_id, live = enqueue_reconciliation(claim["id"], note)
                if task_id:
                    enqueued += 1
                    print(f"    -> reconciliation arbitration enqueued ({task_id})")
                elif live:
                    print(f"    -> claim already has a live attack — not re-enqueued")
        else:
            reconciled += 1
            verdict = 0
            if args.verbose:
                print(f"  claim {claim['id']}: reconciled "
                      f"(conf={review['confidence']:.2f}, votes={review['votes']}) "
                      f"{review['reason'][:120]}")
        if not args.dry_run:
            if verdict is not None:
                conn.execute("UPDATE knowledge_claims SET scope_conflict = ? WHERE id = ?",
                             (verdict, claim["id"]))
            conn.execute("""
                INSERT INTO claim_reconciliation_reviews
                (claim_id, reviewed_at, evidence_fingerprint, conflict, conflict_type,
                 confidence, reason, scopes_seen, enqueued_task, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (claim["id"], time.time(), c["fingerprint"], verdict,
                 review["conflict_type"] if review else None,
                 review["confidence"] if review else None,
                 (review.get("reason") or "")[:500] if review else "review failed",
                 len(c["scopes"]), task_id, "deepseek/deepseek-v4-flash"))
            conn.commit()

    print(f"\nReviewed {len(cands)} claim(s): {reconciled} reconciled, "
          f"{flagged} unreconciled, {failed} failed, {enqueued} arbitration task(s) enqueued")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
