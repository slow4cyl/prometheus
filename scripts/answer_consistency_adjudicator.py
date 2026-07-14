#!/usr/bin/env python3
"""answer_consistency_adjudicator.py — detect contradiction hidden inside agreement.

Why (2026-07-01 audit): contradiction detection was VERDICT-level only
(contradiction_detector.py fires on disagreed replications or supported=1 vs
supported=0). Two CONFIRMED results asserting INCOMPATIBLE answers counted as
pure agreement — claim #67000 ("optimal CV vs beta") accumulated 12 "supports"
whose closed forms contradict each other (CV* = sqrt(b^2+1)-b, DEcreasing in b,
vs CV* = 0.284+0.966*sqrt(b), INcreasing in b), and the maturity ladder read
that as replication.

This script runs BEFORE the contradiction detector each cycle and checks, for
every claim at or near a tier promotion, whether its supporting experiments
assert mutually consistent ANSWERS — direction of effect, functional form,
magnitude — not merely matching verdict labels. Incompatible answers bump
contradiction_count and set DISPUTED (same semantics as
detect_worker_contradictions), which the maturity recompute then enforces.

Cost control: only claims whose supporting-evidence fingerprint changed since
the last adjudication are examined (state in answer_adjudications), capped at
--limit per run. LLM: deepseek-v4-flash via OpenRouter (same pattern as
semantic_claim_matcher.py). The adjudicator is instructed to default to
"consistent" unless it can QUOTE the incompatible assertions — a false
DISPUTED is recoverable but noisy.

Usage:
    python3 answer_consistency_adjudicator.py [--dry-run] [--limit N]
                                              [--claim ID] [--verbose]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import socket
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db

ENV_PATH = os.path.expanduser("~/.hermes/.env")
MODEL = "deepseek/deepseek-v4-flash"
MAX_SUPPORTS = 12          # findings sent to the adjudicator per claim
FINDING_CHARS = 700        # per-finding truncation
CONTRADICTION_CAP = 3      # max contradiction_count bump per adjudication
LLM_TIMEOUT_S = 90         # per-call socket timeout. Measured real adjudications on this
                           # model span 17-167s (avg ~74s): 90s catches the majority while
                           # a hard bound keeps the cron under its kill. Slow calls that
                           # exceed it fail fast (no retry) and are re-tried next cycle.
CRON_KILL_S = 300          # the cron ticker SIGKILLs the script at this wall-clock
SAFETY_MARGIN_S = 35       # leave this much slack before the kill
WORST_CALL_S = LLM_TIMEOUT_S + 15       # worst-case single adjudication (timeout + one fast-fail retry + slop)
# Don't START a new claim unless a worst-case call can still finish before the kill.
# Old bug: a fixed 150s "stop" left the last call running to ~275s, and any extra
# latency tipped the whole cron over 300s and it was SIGKILLed mid-write.
START_DEADLINE_S = CRON_KILL_S - SAFETY_MARGIN_S - WORST_CALL_S   # = 195s
MAX_FAILED_TRIES = 3       # give up on a fingerprint after this many failed adjudications


def load_api_key():
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("OPENROUTER_API_KEY", "")


def call_llm(prompt, max_tokens=1200, temperature=0.1):
    api_key = load_api_key()
    if not api_key:
        print("ERROR: no OPENROUTER_API_KEY", file=sys.stderr)
        return None
    data = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system",
             "content": "You are a rigorous scientific adjudicator. You compare "
                        "experimental findings for ANSWER-level consistency and "
                        "report strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://hermes-agent.local"},
        method="POST")
    # Worst case per call is WORST_CALL_S; the main loop's START_DEADLINE_S
    # guarantees a call started at the deadline still finishes before CRON_KILL_S.
    # A TIMEOUT is never retried: the model is genuinely slow, a retry just burns
    # another LLM_TIMEOUT_S and risks the cron kill — defer that claim to the next
    # cycle instead. Only fast transient errors (connection reset) get one retry.
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
                result = json.loads(resp.read())
                return result["choices"][0]["message"]["content"]
        except (TimeoutError, socket.timeout) as e:
            print(f"  LLM call timed out ({LLM_TIMEOUT_S}s) — deferring: {e}", file=sys.stderr)
            return None
        except (urllib.error.URLError, json.JSONDecodeError,
                ConnectionResetError, KeyError) as e:
            # URLError can WRAP a socket timeout — treat that as slow, don't retry.
            if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)) \
                    or "timed out" in str(e).lower():
                print(f"  LLM call timed out (wrapped) — deferring: {e}", file=sys.stderr)
                return None
            if attempt == 1:
                print(f"  LLM call failed after 2 attempts: {e}", file=sys.stderr)
                return None
            time.sleep(3)
    return None


def ensure_state_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS answer_adjudications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            adjudicated_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            n_supports INTEGER,
            consistent INTEGER,           -- 1 yes / 0 contradictory / NULL parse-or-API failure
            incompatibilities TEXT,       -- JSON [{a, b, reason}]
            summary TEXT,
            model TEXT
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_adj_claim_fp
        ON answer_adjudications(claim_id, evidence_fingerprint)""")
    conn.commit()


def get_candidates(conn, limit, only_claim=None):
    """Claims at/near tier promotion whose support set changed since last
    adjudication. Near-promotion = past the REPLICATED wsc bar (2.0 in
    maturity.MATURITY_THRESHOLDS) — deliberately NOT gated on retests:
    claim 67000 sat at wsc 6.5 / 12 supports / 0 retests, one retest away
    from promoting on contradictory evidence. Adjudicate before that."""
    where_claim = f"AND kc.id = {int(only_claim)}" if only_claim else ""
    rows = conn.execute(f"""
        SELECT kc.id, kc.hypothesis_text, kc.claim_status,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM knowledge_claims kc
        WHERE (kc.claim_status IN ('REPLICATED', 'ESTABLISHED')
               OR (kc.claim_status = 'CANDIDATE'
                   AND COALESCE(kc.weighted_support_count, 0) >= 2.0))
          AND COALESCE(kc.contradiction_count, 0) = 0
          {where_claim}
        ORDER BY CASE kc.claim_status
                     WHEN 'ESTABLISHED' THEN 0
                     WHEN 'REPLICATED' THEN 1
                     ELSE 2 END,
                 kc.weighted_support_count DESC
    """).fetchall()

    out = []
    for row in rows:
        evid = conn.execute("""
            SELECT ce.experiment_id,
                   COALESCE(NULLIF(TRIM(ce.key_finding), ''), e.result) AS finding,
                   ce.confidence
            FROM claim_evidence ce
            LEFT JOIN experiments e ON e.id = ce.experiment_id
            WHERE ce.claim_id = ?
              AND COALESCE(ce.evidence_type, 'support') = 'support'
            ORDER BY ce.id DESC
        """, (row["id"],)).fetchall()
        evid = [dict(experiment_id=r["experiment_id"], finding=r["finding"],
                     confidence=r["confidence"])
                for r in evid if r["finding"] and len(str(r["finding"]).strip()) > 20]
        if len(evid) < 2:
            continue
        fp = hashlib.md5(
            ("|".join(sorted(e["experiment_id"] or "" for e in evid))
             + f"#{len(evid)}").encode()).hexdigest()
        seen = conn.execute(
            "SELECT 1 FROM answer_adjudications WHERE claim_id = ? "
            "AND evidence_fingerprint = ? AND consistent IS NOT NULL LIMIT 1",
            (row["id"], fp)).fetchone()
        if seen and not only_claim:
            continue
        # A fingerprint that failed MAX_FAILED_TRIES times is parked until the
        # evidence changes (new fingerprint) — no infinite retry burn.
        failed_tries = conn.execute(
            "SELECT COUNT(*) FROM answer_adjudications WHERE claim_id = ? "
            "AND evidence_fingerprint = ? AND consistent IS NULL",
            (row["id"], fp)).fetchone()[0]
        if failed_tries >= MAX_FAILED_TRIES and not only_claim:
            continue
        out.append({"claim": row, "evidence": evid[:MAX_SUPPORTS],
                    "fingerprint": fp, "n_total": len(evid)})
        if len(out) >= limit:
            break
    return out


def build_prompt(claim_text, evidence):
    lines = []
    for e in evidence:
        f = str(e["finding"]).strip().replace("\n", " ")[:FINDING_CHARS]
        lines.append(f"- [{e['experiment_id']}] {f}")
    findings = "\n".join(lines)
    return f"""CLAIM under adjudication:
"{claim_text}"

The {len(evidence)} experiment findings below are all recorded as SUPPORTING this claim.
Matching verdict labels (CONFIRMED/SUPPORTED) do NOT count as agreement. Compare the actual
ANSWERS each finding asserts: direction of effect, functional/closed form, magnitude of the
same quantity, claimed mechanism.

Findings:
{findings}

Two findings are INCOMPATIBLE only if they cannot both be true of the same question — e.g.
one says the optimum DEcreases in a parameter and another says it INcreases; different closed
forms with opposite behavior; the same quantity differing by more than ~2x under equivalent
conditions; mutually exclusive mechanisms. Findings that address different facets, ranges, or
are too vague to compare are CONSISTENT by default (absence of contradiction is not evidence
of contradiction).

DEFAULT TO consistent unless you can QUOTE the incompatible assertions from both findings.

Answer with STRICT JSON only, no prose outside the JSON:
{{"consistent": true|false,
  "incompatibilities": [{{"a": "exp_id", "b": "exp_id", "reason": "quoted assertions + why incompatible"}}],
  "summary": "one line"}}"""


def parse_verdict(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(v, dict) or "consistent" not in v:
        return None
    v["incompatibilities"] = [
        i for i in (v.get("incompatibilities") or [])
        if isinstance(i, dict) and i.get("a") and i.get("b") and i.get("reason")
    ]
    # A "contradictory" verdict with zero concrete pairs is not actionable.
    if not v["consistent"] and not v["incompatibilities"]:
        v["consistent"] = True
        v["summary"] = (v.get("summary") or "") + " [no concrete pair cited - treated as consistent]"
    return v


def record(conn, claim_id, fp, n_supports, verdict):
    conn.execute("""
        INSERT INTO answer_adjudications
        (claim_id, adjudicated_at, evidence_fingerprint, n_supports,
         consistent, incompatibilities, summary, model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (claim_id, time.time(), fp, n_supports,
         None if verdict is None else (1 if verdict["consistent"] else 0),
         json.dumps(verdict.get("incompatibilities", [])) if verdict else None,
         (verdict.get("summary") or "")[:500] if verdict else "adjudication failed",
         MODEL))


def apply_contradiction(conn, claim, verdict):
    """Same semantics as detect_worker_contradictions: bump the count (capped)
    and set DISPUTED; the maturity recompute keeps it there."""
    bump = min(len(verdict["incompatibilities"]), CONTRADICTION_CAP)
    conn.execute(
        "UPDATE knowledge_claims SET claim_status = 'DISPUTED', "
        "contradiction_count = COALESCE(contradiction_count, 0) + ?, "
        "last_updated_at = ? WHERE id = ?",
        (bump, time.time(), claim["id"]))
    print(f"DISPUTED claim {claim['id']} [{claim['claim_status']}] "
          f"(+{bump} contradictions): '{(claim['hypothesis_text'] or '')[:80]}'")
    for inc in verdict["incompatibilities"][:CONTRADICTION_CAP]:
        print(f"    {inc['a']} vs {inc['b']}: {inc['reason'][:160]}")


def main():
    ap = argparse.ArgumentParser(description="Answer-level consistency adjudication at tier promotion")
    ap.add_argument("--dry-run", action="store_true", help="Adjudicate but write nothing")
    ap.add_argument("--limit", type=int, default=12, help="Max claims per run")
    ap.add_argument("--claim", type=int, default=None, help="Force one claim id (ignores fingerprint dedup)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    ensure_state_table(conn)
    cands = get_candidates(conn, args.limit, only_claim=args.claim)
    if not cands:
        print("No claims need adjudication (all fingerprints current).")
        conn.close()
        return 0

    disputed = consistent = failed = 0
    t0 = time.time()
    for c in cands:
        if time.time() - t0 > START_DEADLINE_S:
            print(f"  start deadline ({START_DEADLINE_S}s) reached — a worst-case "
                  f"call ({WORST_CALL_S}s) would risk the {CRON_KILL_S}s cron kill; "
                  f"deferring remaining claims to the next cycle")
            break
        claim = c["claim"]
        prompt = build_prompt(claim["hypothesis_text"] or "", c["evidence"])
        if args.verbose:
            print(f"--- claim {claim['id']} ({claim['claim_status']}, "
                  f"{len(c['evidence'])}/{c['n_total']} supports) ---")
        verdict = parse_verdict(call_llm(prompt))
        if verdict is None:
            failed += 1
            print(f"  claim {claim['id']}: adjudication FAILED (API/parse)")
        elif verdict["consistent"]:
            consistent += 1
            if args.verbose:
                print(f"  claim {claim['id']}: consistent — {verdict.get('summary','')[:120]}")
        else:
            disputed += 1
            if args.dry_run:
                print(f"[DRY-RUN] would DISPUTE claim {claim['id']}: "
                      f"{json.dumps(verdict['incompatibilities'])[:300]}")
            else:
                apply_contradiction(conn, claim, verdict)
        if not args.dry_run:
            record(conn, claim["id"], c["fingerprint"], c["n_total"], verdict)
            conn.commit()

    print(f"\nAdjudicated {len(cands)} claim(s): {consistent} consistent, "
          f"{disputed} disputed, {failed} failed")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
