#!/usr/bin/env python3
"""recall_audit.py — the missing symmetric audit: verify the KNOWN bin.

`novelty_calibration --adjudicate` audits the NOVEL shelf (are the claims we call
novel actually novel?). Nothing audited the INVERSE: claims routed OFF the shelf
because a finder produced a prior-work citation. That routing is permanent
(`discovery_routing.route_claim`: any `prior_work_citation` set → KNOWN_IN_LIT,
no recheck), and the finder fabricates a measured ~24% of its KNOWNs
(scholar_recheck post-pass: 7/29 false — hallucinated papers, citations to the
system's OWN internal work/experiment ids, adjacent-keyword collisions). So a
bogus citation silently deletes a real discovery, and only a human post-pass has
ever caught it.

This audits that bin, in two layers:
  MECHANICAL (no LLM) — a citation that references the system's own internal work
    (an experiment id, 'Claim NNNNN', 'Internal … Repository', a search engine)
    or is a non-locatable hand-wave ('Multiple sources') FAILS outright. Catches
    the circular + invalid-form classes (#63438, #68314, #59310) for free.
  SEMANTIC (index-armed judge) — for the rest, the judge sees the claim, the
    finder's citation, and real Semantic Scholar/OpenAlex hits, and rules whether
    the cited work ACTUALLY contains this claim's specific finding (not just the
    topic). Adjacent-but-wrong — a real paper cited for a result it doesn't
    contain (#65514, #70232) — FAILS here, where no mechanical check can reach.

A FAILS un-bins the claim: append-only NOT_FOUND row (`corroborated=1`, citation
cleared, `model='recall_audit'`) — the exact revert path used for the 7 manual
reverts — restoring its honest 'searched, found nothing real' state. A HOLDS is
recorded so it is not re-audited. Ledger `recall_audits`, one row per audited
KNOWN audit row; dedup key = that audit row's id, so each finder KNOWN is checked
exactly once and a newly-promoted KNOWN is picked up next pass (re-binning is
safe — a fresh KNOWN is a fresh audit id).

Scope: FINDER KNOWNs only (`finder_found IS NOT NULL`) — the measured hole. The
primary-audit model's own KNOWNs are a separate, more reliable population;
`--include-audit-knowns` widens to them later.

Backfill + steady-state in one: the first run clears the existing bin; the cron
keeps it clean going forward.

Usage:
  recall_audit.py                        # count the bin, no network
  recall_audit.py --run --limit 20       # audit up to 20 (promotion-band first)
  recall_audit.py --run                  # whole bin
  recall_audit.py --history              # ledger summary
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import novelty_audit as na            # DB, API, extract_json, generate_search_queries, _mechanical_query
import scholar_search
from _key_helper import get_key

DB = na.DB
API = na.API
# strong judge deliberately: an un-bin re-shelves a claim, a wrong HOLDS leaves a
# discovery buried — both directions matter, so this rides the full-flash judge,
# the same one the novelty ceiling rests on (openai/* is account-blocked).
JUDGE_MODEL = os.environ.get("HERMES_RECALL_JUDGE", "google/gemini-3.5-flash")

# ---- mechanical layer -----------------------------------------------------
# self-reference: the finder cited the system's OWN work, an internal experiment
# id, or a search engine — never valid external prior work (#63438, #68314).
_SELF_REF = re.compile(
    r"\bclaim\s+\d{4,}\b|internal\s+audit|replication\s+repositor|audit\s*/\s*replication|"
    r"vertex\s+ai\s+search|\bP\d{2}_\d{2}\b|\bexp_[a-z0-9]{4,}|residual\s+audit|"
    r"coale\.science|internal\s+(?:repo|repositor|database|ledger|index)", re.I)
# invalid form: a hand-wave that names no locatable work (#59310).
_INVALID_FORM = re.compile(
    r"\bmultiple\s+sources\b|\b(?:several|various|numerous)\s+"
    r"(?:papers|studies|works|sources|articles)\b", re.I)


def mechanical_verdict(citation):
    """(fails, relationship, reason). fails=False ⇒ escalate to the judge."""
    c = (citation or "").strip()
    if not c or c.lower() in ("null", "none", "n/a"):
        return True, "invalid-form", "KNOWN verdict carries no citation"
    if _SELF_REF.search(c):
        return True, "self-ref", ("citation references the system's own internal work / an "
                                  "experiment id / a search engine, not external prior work")
    if _INVALID_FORM.search(c):
        return True, "invalid-form", ("citation is a non-locatable hand-wave ('multiple/several "
                                      "sources'), not a specific work")
    return False, None, None


# ---- semantic layer -------------------------------------------------------
# The question is NOT "does the finder's exact citation match?" — these claims are
# bespoke experiments, so a genuine counterpart establishes the same RELATIONSHIP,
# never the identical parameters, and demanding an exact match un-bins everything
# (validation caught this: 6/6 wrongly un-binned, incl. #45165 whose Dhar &
# Wertenbroch citation is genuinely correct). The question is "is the claim's CORE
# finding in the literature ANYWHERE, or trivial?" — un-bin ONLY a claim that is
# genuinely novel. This also handles the wrong-citation-but-still-known case
# (#70232: a textbook AR(1) variance fact mis-cited to a GMM paper) — it stays
# binned and the judge supplies a better citation, rather than being mislabeled novel.
def _recall_prompt(claim_summary, domain, citation, hits_block):
    index_part = ""
    if hits_block:
        index_part = f"""

INDEXED PAPERS — real Semantic Scholar + OpenAlex results for this claim (use them to check what the literature actually contains):
{hits_block}"""
    return f"""An autonomous discovery system moved the claim below OFF its novelty shelf, asserting that a literature finder located published PRIOR WORK for it (cited below). Finders are known to sometimes FABRICATE a citation, cite the system's OWN internal work, or cite an ADJACENT paper that shares keywords but reports a different result. Independently determine the TRUTH: is this claim's core finding already present in the published literature — via the cited work OR any other published work — or is it trivial/derivable? Only a claim with NONE of these (genuinely not in the literature, and not trivial) belongs back on the novelty shelf.

Judge the claim's CORE finding — the central relationship, mechanism, boundary, or effect — NOT its incidental experimental parameters. A prior paper that establishes the same relationship in a different setup, dataset, or notation still makes the claim non-novel.

CLAIM: {claim_summary[:900]}
Domain: {domain}

FINDER'S CITATION (verify it — do NOT assume it is correct): {citation[:500]}{index_part}

Answer with ONLY JSON:
{{
  "cited_work_supports_claim": true | false,   // does the SPECIFIC cited work above genuinely contain this claim's core finding?
  "in_literature": true | false,               // is the core finding published ANYWHERE (the cited work OR other prior work)?
  "trivial": true | false,                     // theorem/identity/definition/lookup rather than a contingent finding?
  "best_citation": "the strongest real supporting reference you can confirm, or null",
  "reason": "one sentence",
  "verdict": "KNOWN" | "NOVEL"                 // NOVEL ONLY if in_literature is false AND trivial is false
}}"""


def judge_citation(claim, citation, key):
    """Index-armed re-adjudication of the binned claim's novelty. Returns a dict
    {ok, keep, cited_ok, in_lit, trivial, best_citation, relationship, reason,
    n_hits}. ok=False on error ⇒ leave the KNOWN untouched, re-eligible next pass."""
    import requests
    try:
        queries = na.generate_search_queries(
            {"claim_summary": claim["claim_summary"], "hypothesis_text": claim["hypothesis_text"],
             "domain": claim["domain"]}, key) or [na._mechanical_query(claim)]
        hits = scholar_search.search_indexes(queries)
        body = {"model": JUDGE_MODEL,
                "messages": [{"role": "user", "content": _recall_prompt(
                    claim["claim_summary"] or claim["hypothesis_text"] or "",
                    claim["domain"] or "unclassified", citation,
                    scholar_search.format_hits(hits) if hits else "")}],
                "plugins": [{"id": "web", "max_results": 5}],
                "temperature": 0.1, "max_tokens": 4000}
        resp = requests.post(API, json=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=240)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content") or "").strip() or (msg.get("reasoning") or "")
        j = na.extract_json(content) or {}
        in_lit = bool(j.get("in_literature"))
        trivial = bool(j.get("trivial"))
        v = str(j.get("verdict", "") or "").strip().upper()
        # keep OFF the shelf if the core finding is published anywhere OR trivial;
        # trust the structured fields, but a NOVEL token overrides a stray in_lit=true
        keep = (in_lit or trivial) and v != "NOVEL"
        best = str(j.get("best_citation") or "").strip()
        if best.lower() in ("null", "none", ""):
            best = None
        return {"ok": True, "keep": keep, "cited_ok": bool(j.get("cited_work_supports_claim")),
                "in_lit": in_lit, "trivial": trivial, "best_citation": best,
                "relationship": ("trivial" if trivial else "in-literature" if keep else "not-found"),
                "reason": str(j.get("reason", ""))[:400], "n_hits": len(hits)}
    except Exception as e:  # noqa: BLE001 — an error must not un-bin a claim
        return {"ok": False, "reason": f"judge error: {str(e)[:150]}", "n_hits": 0}


# ---- DB -------------------------------------------------------------------
def ensure_ledger(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recall_audits (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER NOT NULL,
            audit_id INTEGER NOT NULL,
            layer TEXT NOT NULL,
            verdict TEXT NOT NULL,        -- UNBIN (mechanical, acted) | SUSPECT (semantic, flagged) | OK
            relationship TEXT,
            citation TEXT,
            suggested_citation TEXT,
            reason TEXT,
            model TEXT,
            n_index_hits INTEGER,
            created_at REAL NOT NULL)""")
    for col, typ in (("suggested_citation", "TEXT"),):   # forward-compat if table pre-exists
        try:
            conn.execute(f"ALTER TABLE recall_audits ADD COLUMN {col} {typ}")
        except Exception:  # noqa: BLE001
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS ix_recall_audit_id ON recall_audits(audit_id)")
    conn.commit()


def bin_targets(conn):
    """Latest audit per claim that is a FINDER KNOWN still binned LIT_KNOWN and not
    yet recall-audited. Promotion-band first (their binning is what costs a
    discovery), then wsc DESC."""
    conn.row_factory = sqlite3.Row
    return conn.execute("""
        SELECT na.id AS audit_id, na.finder_found, na.citations,
               kc.id AS claim_id, kc.claim_summary, kc.hypothesis_text, kc.domain,
               COALESCE(kc.weighted_support_count,0) AS wsc,
               (kc.claim_status IN ('REPLICATED','ESTABLISHED')
                AND COALESCE(kc.is_meta,0)=0
                AND COALESCE(kc.is_empirical_fact,0)=0) AS promotion_band
        FROM novelty_audits na
        JOIN knowledge_claims kc ON kc.id = na.claim_id
        WHERE na.id = (SELECT MAX(a2.id) FROM novelty_audits a2 WHERE a2.claim_id = na.claim_id)
          AND na.verdict = 'KNOWN'
          AND na.finder_found IS NOT NULL
          AND kc.prior_work_status = 'LIT_KNOWN'
          AND NOT EXISTS (SELECT 1 FROM recall_audits ra WHERE ra.audit_id = na.id)
        ORDER BY promotion_band DESC, wsc DESC
    """).fetchall()


def citation_of(row):
    """The finder's citation for this KNOWN — finder_found, else first citations[].ref."""
    if (row["finder_found"] or "").strip():
        return row["finder_found"].strip()
    try:
        arr = json.loads(row["citations"] or "[]")
        if arr and isinstance(arr, list):
            return str((arr[0] or {}).get("ref", "")).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def unbin(conn, claim_id, layer, rel, reason):
    """Append-only un-bin: NOT_FOUND row (corroborated=1) + restore LIT_NOT_FOUND +
    clear the citation. The same revert path used for the manual reverts."""
    conn.execute("""
        INSERT INTO novelty_audits
        (claim_id, verdict, confidence, citations, explanation, novel_residue,
         model, web_used, created_at, corroborated, finder_model, finder_found, search_adequacy)
        VALUES (?, 'NOT_FOUND', 0.7, '[]', ?, NULL, 'recall_audit', 1, ?, 1,
                'recall_audit', NULL, 0.9)""",
        (claim_id,
         f"recall_audit un-bin [{layer}/{rel}]: {reason} Finder's prior-work citation was invalid "
         f"(fabricated / self-referential / non-locatable) — restored from LIT_KNOWN to "
         f"searched-not-found (corroborated=1).", time.time()))
    conn.execute("UPDATE knowledge_claims SET prior_work_status='LIT_NOT_FOUND', "
                 "prior_work_citation=NULL WHERE id=?", (claim_id,))


def record_and_act(row, layer, res, citation, model):
    """Ledger the recall verdict. MECHANICAL FAILS auto-un-bins (regex on citation
    form — self-referential / non-locatable — is high-precision and unambiguous).
    SEMANTIC only FLAGS: the judge is NOT reliable enough to auto-act — validation
    showed it mislabels known-relationship claims as novel, especially at idx=0
    (pure model recall), which would trade false-KNOWN for false-NOVEL. So a
    semantic 'novel' opinion is recorded as SUSPECT for operator review (see
    --report / --confirm), never acted on autonomously. Returns the ledger verdict."""
    rel = res.get("relationship")
    reason = res.get("reason", "")
    n_hits = res.get("n_hits", 0)
    best = res.get("best_citation")
    if layer == "mechanical":
        verdict = "UNBIN"
    else:
        verdict = "OK" if res["keep"] else "SUSPECT"
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("""
            INSERT INTO recall_audits
            (claim_id, audit_id, layer, verdict, relationship, citation, suggested_citation,
             reason, model, n_index_hits, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["claim_id"], row["audit_id"], layer, verdict, rel, (citation or "")[:400],
             (best[:400] if best else None), reason, model, n_hits, time.time()))
        if verdict == "UNBIN":
            unbin(conn, row["claim_id"], layer, rel, reason)
        conn.commit()
        return verdict
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually audit (network)")
    ap.add_argument("--limit", type=int, default=0, help="0 = whole bin")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--history", action="store_true", help="ledger summary, no network")
    ap.add_argument("--report", action="store_true",
                    help="list SUSPECT flags awaiting operator review, no network")
    ap.add_argument("--confirm", type=int, nargs="+", metavar="CLAIM_ID",
                    help="un-bin these claim ids (operator-confirmed SUSPECTs)")
    ap.add_argument("--time-budget", type=int, default=0,
                    help="stop launching new audits after N seconds (cron safety; 0=off)")
    ap.add_argument("--mechanical-only", action="store_true",
                    help="run ONLY the high-precision mechanical layer (no LLM/index, no network) "
                         "— auto-un-bins fabricated/self-referential citations, skips the noisy "
                         "semantic judge. The cron-safe forward guard.")
    args = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_ledger(conn)

    if args.history:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT verdict, layer, COUNT(*) c FROM recall_audits GROUP BY verdict, layer "
            "ORDER BY verdict, layer").fetchall()
        total = conn.execute("SELECT COUNT(*) FROM recall_audits").fetchone()[0]
        print(f"recall_audits ledger: {total} audited")
        for r in rows:
            print(f"  {r['verdict']:8s} [{r['layer']:10s}] {r['c']}")
        conn.close()
        return 0

    if args.report:
        # the review queue: current SUSPECT flags on claims STILL binned (a later
        # cron may have superseded), promotion-band first
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT ra.claim_id, ra.reason, ra.citation, ra.suggested_citation, ra.n_index_hits,
                   substr(kc.claim_summary,1,150) AS summary,
                   (kc.claim_status IN ('REPLICATED','ESTABLISHED') AND COALESCE(kc.is_meta,0)=0
                    AND COALESCE(kc.is_empirical_fact,0)=0) AS pb
            FROM recall_audits ra JOIN knowledge_claims kc ON kc.id=ra.claim_id
            WHERE ra.verdict='SUSPECT' AND kc.prior_work_status='LIT_KNOWN'
              AND ra.id=(SELECT MAX(id) FROM recall_audits r2 WHERE r2.claim_id=ra.claim_id)
            ORDER BY pb DESC, ra.created_at DESC""").fetchall()
        print(f"SUSPECT review queue: {len(rows)} finder-KNOWNs the judge flagged as possibly novel")
        print("(auto-un-bin is NOT applied to these — confirm with --confirm <id ...>)\n")
        for r in rows:
            print(f"  #{r['claim_id']}{' [PB]' if r['pb'] else ''} idx={r['n_index_hits']}")
            print(f"     claim : {r['summary']}")
            print(f"     cited : {(r['citation'] or '')[:110]}")
            print(f"     judge : {(r['reason'] or '')[:150]}")
            if r["suggested_citation"]:
                print(f"     better?: {r['suggested_citation'][:110]}")
            print()
        conn.close()
        return 0

    if args.confirm:
        done = []
        for cid in args.confirm:
            flagged = conn.execute(
                "SELECT relationship, reason FROM recall_audits WHERE claim_id=? AND verdict='SUSPECT' "
                "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            binned = conn.execute("SELECT prior_work_status FROM knowledge_claims WHERE id=?",
                                  (cid,)).fetchone()
            if not flagged:
                print(f"  #{cid}: no SUSPECT flag — skipping")
                continue
            if not binned or binned[0] != 'LIT_KNOWN':
                print(f"  #{cid}: not currently LIT_KNOWN (state={binned[0] if binned else 'gone'}) — skipping")
                continue
            unbin(conn, cid, "operator-confirmed", flagged[0] or "semantic",
                  f"operator-confirmed SUSPECT: {flagged[1] or ''}")
            conn.execute("INSERT INTO recall_audits (claim_id, audit_id, layer, verdict, "
                         "relationship, reason, model, n_index_hits, created_at) "
                         "VALUES (?, 0, 'operator', 'UNBIN', ?, ?, 'operator-confirmed', 0, ?)",
                         (cid, flagged[0], f"operator-confirmed un-bin", time.time()))
            done.append(cid)
            print(f"  #{cid}: un-binned (operator-confirmed)")
        conn.commit()
        conn.close()
        print(f"\n{len(done)} un-binned; spotlight re-ranks them next --apply.")
        return 0

    rows = bin_targets(conn)
    conn.close()
    n_pb = sum(1 for r in rows if r["promotion_band"])
    print(f"KNOWN bin: {len(rows)} finder-KNOWNs un-audited ({n_pb} promotion-band, audited first)")
    if not args.run:
        print("dry: pass --run to audit (judge: %s)" % JUDGE_MODEL)
        return 0
    if args.limit:
        rows = rows[:args.limit]

    key = get_key()
    counts = {"UNBIN": 0, "SUSPECT": 0, "OK": 0, "error": 0}
    t0 = time.time()
    deadline = (t0 + args.time_budget) if args.time_budget else None

    def work(row):
        citation = citation_of(row)
        fails, rel, reason = mechanical_verdict(citation)
        if fails:
            return row, "mechanical", citation, {"ok": True, "keep": False, "relationship": rel,
                                                 "reason": reason, "n_hits": 0}
        if args.mechanical_only:
            # passed the mechanical gate — leave UNrecorded so a later semantic --run
            # still examines it; skip the noisy judge entirely
            return row, "mechanical", citation, {"ok": True, "skip": True}
        return row, "semantic", citation, judge_citation(row, citation, key)

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(work, r) for r in rows]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                row, layer, citation, res = fut.result()
            except Exception as e:  # noqa: BLE001
                counts["error"] += 1
                print(f"  !! worker exception: {str(e)[:120]}")
                continue
            if res.get("skip"):            # mechanical-only passer → not recorded
                continue
            if not res.get("ok"):          # judge error → leave untouched, re-eligible
                counts["error"] += 1
                print(f"  [{i}/{len(rows)}] #{row['claim_id']} judge-error (untouched) — {res.get('reason','')[:70]}")
                continue
            model = "mechanical" if layer == "mechanical" else JUDGE_MODEL
            verdict = record_and_act(row, layer, res, citation, model)
            counts[verdict] = counts.get(verdict, 0) + 1
            flag = "PB " if row["promotion_band"] else "   "
            mark = {"UNBIN": "✗ UN-BIN (auto)", "SUSPECT": "⚑ SUSPECT (review)",
                    "OK": "✓ ok"}[verdict]
            print(f"  [{i}/{len(rows)}] {flag}#{row['claim_id']} {mark} "
                  f"[{layer}/{res.get('relationship')}] idx={res.get('n_hits',0)}"
                  f" — {res.get('reason','')[:80]}")
            if deadline and time.time() > deadline:
                print(f"  (time budget {args.time_budget}s reached — {len(rows)-i} left for next pass)")
                break

    dt = time.time() - t0
    print(f"\ndone in {dt/60:.1f} min: {counts}")
    print(f"AUTO un-binned {counts['UNBIN']} (mechanical: fabricated/self-referential citations) → "
          f"restored to shelf; flagged {counts['SUSPECT']} SUSPECT for review "
          f"(`recall_audit.py --report`); {counts['OK']} citations confirmed OK. "
          f"Semantic flags are NOT auto-applied — the judge isn't calibrated to act alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
