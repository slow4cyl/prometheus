#!/usr/bin/env python3
"""circularity_critic.py — block promotion of self-fulfilling constructions.

Why (2026-07-01 audit): the June ESTABLISHED claim #3839 rested on
exp_crossmodal_adversarial_transfer.py, which FABRICATES "adversarial"
audio/text by boosting entropy / high-frequency energy / peak density, then
"detects" it by extracting those same statistics as features. The label is
defined by the features used to detect it — the experiment cannot fail. The
spurious-agreement gate cannot catch this: SA measures whether supports are
DIVERSE, and twelve independently-run circular experiments look diverse.

This critic examines claims in the promotion band and asks one question of
the supporting experiments' construction: is the target variable constructed
from the same features used to detect it, or the data manufactured to contain
the property being tested? A confident YES sets
knowledge_claims.circular_construction = 1, which compute_maturity() treats
as a cap at CANDIDATE (see maturity.py).

Evidence examined per experiment, best-first:
  1. archived code from ~/.hermes/artifacts/<task_id|exp_id>/ (preserved at
     result-write time since 2026-07-01 — artifact_preserve.py)
  2. the hypothesis + finding text (the fabricate-and-detect pattern is often
     visible there for older, GC'd experiments)

Conservative by construction: flags only with quoted evidence and
critic-confidence >= 0.7; anything less records 0 (clean) so the claim is
not re-examined until its evidence fingerprint changes.

Usage:
    python3 circularity_critic.py [--dry-run] [--limit N] [--claim ID] [--verbose]
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
from answer_consistency_adjudicator import call_llm  # same OpenRouter plumbing

MAX_EXPERIMENTS = 3       # supporting experiments examined per claim
CODE_CHARS = 6000         # per-experiment code excerpt
FLAG_CONFIDENCE = 0.7     # critic confidence required to flag


def ensure_state(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS circularity_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            reviewed_at REAL NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            circular INTEGER,             -- 1 flagged / 0 clean / NULL failed
            confidence REAL,
            reason TEXT,
            code_seen INTEGER,            -- how many experiments had archived code
            model TEXT
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_circ_claim_fp
        ON circularity_reviews(claim_id, evidence_fingerprint)""")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")]
    if "circular_construction" not in cols:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN circular_construction INTEGER")
    conn.commit()


def find_archived_code(hermes, exp_id, kanban_task_id):
    """Return (path, code_excerpt) for the experiment's main archived .py, or
    (None, None). Searches artifacts/<task_id>/, artifacts/<exp_id>/, then the
    persistent experiments/ dir by exp id."""
    roots = []
    art = os.path.join(hermes, "artifacts")
    if kanban_task_id:
        roots.append(os.path.join(art, str(kanban_task_id)))
    if exp_id:
        roots.append(os.path.join(art, str(exp_id)))
    for d in roots:
        if not os.path.isdir(d):
            continue
        pys = sorted(
            (p for p in os.listdir(d) if p.endswith(".py")),
            key=lambda p: -os.path.getsize(os.path.join(d, p)))
        if pys:
            p = os.path.join(d, pys[0])
            try:
                with open(p, errors="replace") as f:
                    return p, f.read(CODE_CHARS)
            except OSError:
                pass
    # persistent experiments dir: exp_<id>*.py written by well-behaved workers
    if exp_id:
        expdir = os.path.join(hermes, "experiments")
        if os.path.isdir(expdir):
            cands = [p for p in os.listdir(expdir)
                     if p.startswith(str(exp_id)) and p.endswith(".py")]
            if cands:
                p = os.path.join(expdir, sorted(cands)[0])
                try:
                    with open(p, errors="replace") as f:
                        return p, f.read(CODE_CHARS)
                except OSError:
                    pass
    return None, None


def get_candidates(conn, limit, only_claim=None):
    where_claim = f"AND kc.id = {int(only_claim)}" if only_claim else ""
    rows = conn.execute(f"""
        SELECT kc.id, kc.hypothesis_text, kc.claim_status,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM knowledge_claims kc
        WHERE (kc.claim_status IN ('REPLICATED', 'ESTABLISHED')
               OR (kc.claim_status = 'CANDIDATE'
                   AND COALESCE(kc.weighted_support_count, 0) >= 2.0))
          AND kc.circular_construction IS NULL
          {where_claim}
        ORDER BY CASE kc.claim_status
                     WHEN 'ESTABLISHED' THEN 0
                     WHEN 'REPLICATED' THEN 1
                     ELSE 2 END,
                 kc.weighted_support_count DESC
    """).fetchall()

    hermes = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = []
    for row in rows:
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
        evid = [r for r in evid
                if r["finding"] and len(str(r["finding"]).strip()) > 20]
        if not evid:
            continue
        fp = hashlib.md5(
            ("|".join(sorted(r["experiment_id"] or "" for r in evid))
             + f"#{len(evid)}").encode()).hexdigest()
        seen = conn.execute(
            "SELECT 1 FROM circularity_reviews WHERE claim_id = ? "
            "AND evidence_fingerprint = ? AND circular IS NOT NULL LIMIT 1",
            (row["id"], fp)).fetchone()
        if seen and not only_claim:
            continue
        exps = []
        for r in evid[:MAX_EXPERIMENTS]:
            path, code = find_archived_code(hermes, r["experiment_id"],
                                            r["kanban_task_id"])
            exps.append({"experiment_id": r["experiment_id"],
                         "finding": str(r["finding"])[:900],
                         "code_path": path, "code": code})
        out.append({"claim": row, "exps": exps, "fingerprint": fp})
        if len(out) >= limit:
            break
    return out


def build_prompt(claim_text, exps):
    parts = []
    for e in exps:
        block = f"[{e['experiment_id']}]\nFINDING: {e['finding']}"
        if e["code"]:
            block += f"\nCODE ({os.path.basename(e['code_path'])}):\n```python\n{e['code']}\n```"
        else:
            block += "\nCODE: not preserved (judge from the finding text only)"
        parts.append(block)
    material = "\n\n".join(parts)
    return f"""CLAIM under review:
"{claim_text}"

Supporting experiments (finding text, plus code where preserved):

{material}

QUESTION — is the supporting evidence a CIRCULAR CONSTRUCTION? Circular means the experiment
cannot fail by design, for example:
  - the target/label is DEFINED using the same features later used to detect it
    (e.g. "adversarial" samples fabricated by boosting entropy/HF-energy/peaks, then
    "detected" by extracting entropy/HF-energy/peak features);
  - the data is synthesized to contain exactly the property being tested, then the test
    "finds" that property;
  - the ground truth is derived from the model's own output.

NOT circular: honest synthetic experiments where the target is generated INDEPENDENTLY of the
tested feature (a null/independent generator that can and does produce REFUTED); real external
data; simulations whose outcome is not baked into the construction.

Be conservative: flag circular=true ONLY if you can point to the construction in the material
above (quote it). If the material is too thin to tell, answer circular=false with low confidence.

STRICT JSON only:
{{"circular": true|false, "confidence": 0.0-1.0,
  "reason": "one or two sentences quoting the circular construction (or why it is clean)",
  "experiments_implicated": ["exp_id", ...]}}"""


def parse_review(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(v, dict) or "circular" not in v:
        return None
    try:
        v["confidence"] = float(v.get("confidence", 0.0))
    except (TypeError, ValueError):
        v["confidence"] = 0.0
    return v


def main():
    ap = argparse.ArgumentParser(description="Flag circular (self-fulfilling) constructions before promotion")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--claim", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = get_db()
    ensure_state(conn)
    cands = get_candidates(conn, args.limit, only_claim=args.claim)
    if not cands:
        print("No claims need circularity review.")
        conn.close()
        return 0

    flagged = clean = failed = 0
    t0 = time.time()
    for c in cands:
        if time.time() - t0 > 150:  # cron ticker kills at 300s; worst-case call ~125s
            print("  wall budget reached — deferring remaining claims")
            break
        claim = c["claim"]
        review = parse_review(call_llm(build_prompt(
            claim["hypothesis_text"] or "", c["exps"]), max_tokens=900))
        code_seen = sum(1 for e in c["exps"] if e["code"])
        if review is None:
            failed += 1
            print(f"  claim {claim['id']}: review FAILED (API/parse)")
            verdict = None
        else:
            is_circular = bool(review["circular"]) and review["confidence"] >= FLAG_CONFIDENCE
            verdict = 1 if is_circular else 0
            if is_circular:
                flagged += 1
                tag = "[DRY-RUN] would flag" if args.dry_run else "FLAGGED"
                print(f"{tag} CIRCULAR claim {claim['id']} [{claim['claim_status']}] "
                      f"(conf={review['confidence']:.2f}, code_seen={code_seen}): "
                      f"{review['reason'][:200]}")
            else:
                clean += 1
                if args.verbose:
                    print(f"  claim {claim['id']}: clean "
                          f"(conf={review['confidence']:.2f}, code_seen={code_seen}) "
                          f"{review['reason'][:120]}")
        if not args.dry_run:
            if verdict is not None:
                conn.execute(
                    "UPDATE knowledge_claims SET circular_construction = ? WHERE id = ?",
                    (verdict, claim["id"]))
            conn.execute("""
                INSERT INTO circularity_reviews
                (claim_id, reviewed_at, evidence_fingerprint, circular,
                 confidence, reason, code_seen, model)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (claim["id"], time.time(), c["fingerprint"], verdict,
                 review["confidence"] if review else None,
                 (review.get("reason") or "")[:500] if review else "review failed",
                 code_seen, "deepseek/deepseek-v4-flash"))
            conn.commit()

    print(f"\nReviewed {len(cands)} claim(s): {clean} clean, {flagged} circular, {failed} failed")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
