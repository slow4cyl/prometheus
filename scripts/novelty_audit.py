#!/usr/bin/env python3
"""novelty_audit.py — literature-check promoted claims: known, novel, or contradicted.

Why (2026-07-03): the ladder's gates filter for TRUE (retest → attack →
arbitration) but nothing filters for NEW. The worker fleet mostly rediscovers
results it already knows from training — which is fine (checkable ground truth
is what proves the gates select for truth), but the shelf needs labels saying
WHICH claims are textbook and which have no published counterpart. The old
ground_all_claims.py stamped prior_work_status from hardcoded keyword→citation
tables without ever touching the literature; this lane replaces that with a
real check: deepseek-v4-flash (a different family from the mimo fleet, so its
prior is at least partially independent) + the OpenRouter web plugin, reasoning
effort high.

Verdicts — stored as prior_work_status = 'LIT_'+verdict so they are
provenance-distinct from the regex-era NOVEL/KNOWN/WELL_KNOWN values:
  KNOWN           the specific result is published (citation given)
  PARTIALLY_KNOWN the framework/qualitative result is published; the specific
                  quantitative/boundary content of this claim was not found
  NOT_FOUND       no published counterpart found. This is a CANDIDATE pool,
                  not a prize: most entries are search-misses or trivial
                  corollaries, a few may be real
  CONTRADICTED    published work disagrees with the claim's CORE assertion
                  (not an incidental figure in a supporting detail)

Full provenance (citations, explanation, novel residue, model) lands in the
novelty_audits table. posterior/tier fields are NEVER touched — grounding is
metadata, not evidence (the old script decaying posterior was a bug).

DB discipline: targets are read via a mode=ro connection; the LLM call runs
with NO connection open; each verdict is a short-lived rw connect + commit.
created_at is an epoch float (never ISO — the columns have numeric semantics).

Usage:
  python3 novelty_audit.py --dry-run             # list targets, no calls
  python3 novelty_audit.py --limit 3             # first taste
  python3 novelty_audit.py                       # all eligible ESTABLISHED
  python3 novelty_audit.py --status REPLICATED --limit 20
  python3 novelty_audit.py --claim 63435 --force # re-audit one claim
"""
import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import re

from _key_helper import get_key
from empirical_fact_classifier import is_empirical_fact
import scholar_search

DB = _PP_PROMETHEUS_DB
API = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "deepseek/deepseek-v4-flash"
# The FINDER is the burden-flip: a DIFFERENT family from both the audit model
# (deepseek) and the worker fleet (mimo), tasked to FIND the paper rather than
# confirm its absence. A single self-graded "not found" is weak; a NOT_FOUND that
# also survives an independent model *trying* to find the counterpart is worth
# more, and a finder that DOES surface the paper refutes the novelty outright.
# Fail-soft: any finder error leaves the primary verdict untouched (just
# un-corroborated). Override the model via env if the key's roster differs.
# 2026-07-04: default was openai/gpt-4o-mini, but the OpenRouter account's
# data-policy settings exclude ALL openai/* endpoints (404 "No endpoints
# available matching your guardrail restrictions") — every finder call failed
# fail-soft and corroborated stayed NULL on all 427 audits to date. Gemini is
# a third family (fleet=xiaomi/mimo, audit=deepseek) the policy allows.
# flash-LITE not flash: this is mechanical search-and-cite, not deep reasoning,
# and flash-lite is ~6x cheaper ($0.25/$1.50 per M vs flash's $1.50/$9.00) —
# smoke-tested locating a real citation via the web plugin. The --adjudicate
# JUDGE stays on full flash (novelty_calibration.JUDGE_MODEL): it runs rarely
# and the ceiling number rests on its judgment quality. A systematically weaker
# finder would INFLATE corroborations, which the adjudicate pass is built to catch.
FINDER_MODEL = os.environ.get("HERMES_FINDER_MODEL", "google/gemini-3.1-flash-lite")
FINDER_ENABLED = os.environ.get("HERMES_FINDER", "1") not in ("0", "false", "no")
VERDICTS = ("KNOWN", "PARTIALLY_KNOWN", "NOT_FOUND", "CONTRADICTED")
SUPPORT_SNIPPETS = 3
SLEEP_BETWEEN = 2.0
MAX_TOKENS = 8000


def ro():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def rw():
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_table():
    conn = rw()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS novelty_audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            confidence REAL,
            citations TEXT,       -- JSON array of {ref, covers}
            explanation TEXT,
            novel_residue TEXT,   -- the part of the claim NOT found in prior work
            model TEXT,
            web_used INTEGER DEFAULT 1,
            created_at REAL NOT NULL
        )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_novelty_claim ON novelty_audits(claim_id)")
    # burden-flip provenance: did an independent finder corroborate the absence
    # (both families failed to find it) or refute it (the finder surfaced a paper)?
    for col, decl in (("corroborated", "INTEGER"), ("finder_model", "TEXT"),
                      ("finder_found", "TEXT"), ("search_adequacy", "REAL")):
        try:
            conn.execute(f"ALTER TABLE novelty_audits ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass    # already present
    conn.commit()
    conn.close()


def get_targets(status, limit, claim_id, force):
    conn = ro()
    if claim_id:
        rows = conn.execute("""
            SELECT id, hypothesis_text, claim_summary, domain,
                   COALESCE(weighted_support_count, 0) AS wsc
            FROM knowledge_claims WHERE id = ?""", (claim_id,)).fetchall()
    else:
        skip = "" if force else \
            "AND kc.id NOT IN (SELECT claim_id FROM novelty_audits)"
        rows = conn.execute(f"""
            SELECT kc.id, kc.hypothesis_text, kc.claim_summary, kc.domain,
                   COALESCE(kc.weighted_support_count, 0) AS wsc
            FROM knowledge_claims kc
            WHERE kc.claim_status = ? AND COALESCE(kc.is_meta, 0) = 0
              {skip}
            ORDER BY CAST(kc.weighted_support_count AS REAL) DESC, kc.id
            LIMIT ?""", (status, limit)).fetchall()
    out = []
    for r in rows:
        supports = conn.execute("""
            SELECT experiment_id, key_finding FROM claim_evidence
            WHERE claim_id = ? AND COALESCE(evidence_type,'support') = 'support'
              AND TRIM(COALESCE(key_finding,'')) != ''
            ORDER BY confidence DESC, id DESC LIMIT ?""",
            (r["id"], SUPPORT_SNIPPETS)).fetchall()
        out.append((dict(r), [dict(s) for s in supports]))
    conn.close()
    return out


def build_prompt(claim, supports):
    finding = (claim["claim_summary"] or "").strip()
    hyp = (claim["hypothesis_text"] or "").strip()
    support_lines = "\n".join(
        f"- [{s['experiment_id']}] {str(s['key_finding'])[:350]}" for s in supports)
    return f"""You are auditing one claim from an autonomous experiment system for NOVELTY against the published literature. The system's workers are LLMs running computational experiments; they mostly rediscover known results, and the point of this audit is to label which shelf items are textbook and which have no published counterpart.

CLAIM (survived replication and adversarial attack inside the system):
{finding or hyp}

Question that produced it: {hyp}
Domain: {claim['domain'] or 'unclassified'}
Key supporting findings:
{support_lines or '- (none recorded)'}

Search the web for prior published work — papers, textbooks, established references — covering this SPECIFIC result, not merely the surrounding field. Try the claim's own terms AND the standard names the field would use for the same content.

Then answer with ONLY a JSON object, no prose around it:
{{
  "verdict": "KNOWN" | "PARTIALLY_KNOWN" | "NOT_FOUND" | "CONTRADICTED",
  "confidence": 0.0-1.0,
  "citations": [{{"ref": "Author(s) Year, Title — venue or URL", "covers": "which part of the claim"}}],
  "explanation": "2-4 sentences: what is and is not in the literature",
  "novel_residue": "the specific part of the claim NOT found in prior work, or null"
}}

Verdict rules:
- KNOWN requires a citation covering the specific result (the quantitative relationship, constant, or boundary — not just the topic).
- PARTIALLY_KNOWN: the framework or qualitative version is published, but this claim's specific quantitative/boundary content was not found. Put that content in novel_residue.
- NOT_FOUND: you searched and found no published counterpart. Do NOT use this as praise — say in the explanation whether you suspect search-miss, trivial corollary, or something genuinely absent.
- CONTRADICTED: reserve this for when published work disagrees with the claim's CORE assertion (the finding/answer above) — not merely an incidental figure buried in a supporting detail. If the core assertion is confirmed but a peripheral number is off (e.g. the headline result holds and is published, yet one cited constant or sub-quantity is wrong), use KNOWN or PARTIALLY_KNOWN and record the discrepancy in explanation/novel_residue instead. Cite the disagreeing work.
- If the claim text is too vague to check, use NOT_FOUND with low confidence and say so."""


def call_llm(prompt, key):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "plugins": [{"id": "web", "max_results": 5}],
        "reasoning": {"effort": "high"},
        "temperature": 0.1,
        "max_tokens": MAX_TOKENS,
    }
    headers = {"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    last_err = None
    for attempt, backoff in enumerate((0, 10, 30)):
        if backoff:
            time.sleep(backoff)
        try:
            resp = requests.post(API, json=body, headers=headers, timeout=240)
            if resp.status_code in (429, 500, 502, 503):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            if not content.strip():
                # reasoning models may leave the answer in the reasoning field
                content = msg.get("reasoning") or ""
            usage = data.get("usage", {})
            return content, usage
        except requests.RequestException as e:
            last_err = str(e)[:200]
    raise RuntimeError(f"OpenRouter call failed after retries: {last_err}")


def extract_json(text):
    """First balanced {...} object in the text."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def _mechanical_query(claim):
    """No-LLM fallback query: first sentence of the claim, content words only."""
    txt = (claim.get("claim_summary") or claim.get("hypothesis_text") or "").strip()
    txt = re.sub(r"^ARBITRATION_VERDICT:\s*\w+\.?\s*", "", txt)
    first = re.split(r"(?<=[.!?])\s", txt, 1)[0]
    words = re.findall(r"[A-Za-z][A-Za-z-]{2,}", first)
    drop = {"the", "and", "for", "with", "that", "this", "are", "was", "were",
            "from", "into", "via", "between", "across", "using", "under", "does"}
    return " ".join([w for w in words if w.lower() not in drop][:10])


def generate_search_queries(claim, key, n=3):
    """One cheap FINDER_MODEL pass turning the claim into scholarly-index queries
    that deliberately SPAN DISCIPLINES — the confirmed finder blind spot is a
    counterpart living in another field's vocabulary (#61561: a MARL gridworld
    finding whose prior work is 1960s organization theory). Fail-soft → []
    (caller falls back to _mechanical_query)."""
    finding = (claim.get("claim_summary") or claim.get("hypothesis_text") or "")[:900]
    prompt = f"""A research system needs to find PRIOR PUBLISHED WORK for this claim:

{finding}
Domain: {claim.get('domain') or 'unclassified'}

Write {n} scholarly search queries (each ≤10 words) for paper indexes:
1. the claim's own technical vocabulary
2. the SAME mechanism translated into a neighboring discipline's standard vocabulary (organization science, ecology, epidemiology, economics, statistics — wherever this relationship would classically live)
3. the general name of the phenomenon/relationship

ONLY JSON: {{"queries": ["...", "...", "..."]}}"""
    try:
        body = {"model": FINDER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2, "max_tokens": 3000}
        resp = requests.post(API, json=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=90)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content") or "").strip() or (msg.get("reasoning") or "")
        qs = (extract_json(content) or {}).get("queries") or []
        return [str(q).strip()[:120] for q in qs if str(q).strip()][:4]
    except Exception:  # noqa: BLE001 — query generation must never sink the finder
        return []


def build_finder_prompt(claim, supports, hits=None):
    finding = (claim["claim_summary"] or "").strip()
    hyp = (claim["hypothesis_text"] or "").strip()
    index_block = ""
    if hits:
        index_block = f"""

INDEXED CANDIDATES — actual results from Semantic Scholar + OpenAlex for targeted queries (real papers, not suggestions):
{scholar_search.format_hits(hits)}

Check these FIRST. If any is a counterpart — same relationship, mechanism, boundary, or observation, in ANY discipline's vocabulary or parametrization (a gridworld result can have its prior work in 1960s organization theory) — answer found=true and cite it fully (authors, year, title, venue/DOI). Web search remains available for anything the indexes missed."""
    return f"""You are a literature-search adversary. Another model audited the claim below and reported it has NO published counterpart. Your job is to PROVE THAT WRONG: find the prior published work that already contains this result. Assume it exists and that the other model's search was lazy — search hard before you concede.

CLAIM:
{finding or hyp}
Domain: {claim['domain'] or 'unclassified'}

Search the web using BOTH the claim's own wording AND the standard name the field uses for the same content (the textbook term, the named theorem/law/effect, the canonical author). Try at least two different phrasings. A result counts as a counterpart if it reports the same relationship, constant, boundary, theorem, or observation — even under different notation.{index_block}

Answer with ONLY a JSON object:
{{
  "found": true | false,
  "citation": "Author(s) Year, Title — venue/DOI/URL, or null",
  "confidence": 0.0-1.0,
  "why": "one sentence: what the prior work covers, or what you tried and failed to find"
}}

Return found=true ONLY with a concrete citation you actually located. If after genuine effort you cannot find it, return found=false — that is a real signal, not a failure."""


def find_counterpart(claim, supports, key):
    """Independent, different-family attempt to LOCATE prior work. Returns a dict
    {found, citation, confidence, why, ok, index_hits, queries}. TWO retrieval
    legs: (1) scholarly indexes — LLM-generated cross-disciplinary queries →
    Semantic Scholar + OpenAlex hits the model READS as context (the #61561
    double-miss lesson: web search alone is porous across disciplines), and
    (2) the web plugin as before, for anything unindexed. Fail-soft at every
    seam: index errors → web-only finder, exactly the pre-index behavior; on
    any terminal error ok=False and the caller leaves the primary verdict
    untouched (novelty stays an un-corroborated prior rather than being wrongly
    credited OR wrongly refuted)."""
    if not FINDER_ENABLED:
        return {"ok": False, "found": False, "citation": None, "why": "finder disabled"}
    queries, hits = [], []
    try:
        queries = generate_search_queries(claim, key) or [_mechanical_query(claim)]
        hits = scholar_search.search_indexes(queries)
    except Exception:  # noqa: BLE001 — the index leg must never sink the finder
        pass
    try:
        body = {
            "model": FINDER_MODEL,
            "messages": [{"role": "user", "content": build_finder_prompt(claim, supports, hits)}],
            "plugins": [{"id": "web", "max_results": 5}],
            "temperature": 0.1,
            "max_tokens": MAX_TOKENS,
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        resp = requests.post(API, json=body, headers=headers, timeout=240)
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        content = (msg.get("content") or "").strip() or (msg.get("reasoning") or "")
        parsed = extract_json(content) or {}
        found = bool(parsed.get("found"))
        ref = str(parsed.get("citation") or "").strip()
        if ref.lower() in ("null", "none", ""):
            ref = ""
        # a "found" with no concrete citation is not a find
        found = found and bool(ref)
        try:
            conf = float(parsed.get("confidence"))
        except (TypeError, ValueError):
            conf = None
        return {"ok": True, "found": found, "citation": ref or None,
                "confidence": conf, "why": str(parsed.get("why", ""))[:500],
                "index_hits": len(hits), "queries": queries}
    except Exception as e:  # noqa: BLE001 — finder must never sink the audit
        return {"ok": False, "found": False, "citation": None,
                "why": f"finder error: {str(e)[:150]}", "index_hits": len(hits)}


def write_verdict(claim, parsed, finder=None):
    verdict = str(parsed.get("verdict", "")).strip().upper()
    if verdict not in VERDICTS:
        raise ValueError(f"bad verdict {verdict!r}")
    try:
        conf = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        conf = None
    citations = parsed.get("citations") or []
    residue = parsed.get("novel_residue")
    if isinstance(residue, str) and residue.strip().lower() in ("null", "none", ""):
        residue = None
    first_ref = ""
    if citations and isinstance(citations, list):
        first_ref = str((citations[0] or {}).get("ref", ""))[:400]

    # --- reconcile with the independent finder (the burden flip) ---------
    corroborated = None
    finder_found = None
    if finder and finder.get("ok"):
        if finder.get("found") and verdict in ("NOT_FOUND", "PARTIALLY_KNOWN"):
            # the finder surfaced a paper the audit missed — novelty refuted.
            verdict = "KNOWN"
            corroborated = 0
            finder_found = (finder.get("citation") or "")[:400]
            first_ref = finder_found or first_ref
            residue = None
        elif not finder.get("found") and verdict == "NOT_FOUND":
            # two independent families searched and neither found it — but only
            # credit the absence as CORROBORATED (the full novelty weak-prior) when
            # the STRUCTURED index actually returned papers to check against. The
            # free pools (Semantic Scholar / OpenAlex) 429 and budget-exhaust readily,
            # so a finder can return "found nothing" because it could not look, not
            # because nothing exists — #61497's foundational prior work (Glorot 2010,
            # 19k citations) went un-retrieved under a rate-limit storm and was wrongly
            # corroborated NOVEL. Requiring >=1 real index hit makes "corroborated"
            # mean "searched and absent" rather than "couldn't search"; a blind search
            # leaves novelty an UN-corroborated weak prior (0.4 discount on the shelf),
            # which is also the honest state for a bespoke claim whose exact finding
            # has no indexed referent (the toy-vs-world gap in novelty clothing). This
            # only WITHHOLDS credit — it can never create a false KNOWN.
            if finder.get("index_hits", 0) >= 1:
                corroborated = 1
            else:
                corroborated = 0   # searched but the index engaged nothing → un-corroborated

    # --- close the empirical-fact leak at audit time --------------------
    fact, fact_reason = is_empirical_fact(
        (claim.get("hypothesis_text") or "") + " " + (claim.get("claim_summary") or ""))

    # crude search-adequacy proxy: did the audit actually engage the literature?
    adequacy = 0.6 if (parsed.get("citations") or parsed.get("explanation")) else 0.3
    if corroborated is not None:
        adequacy = min(1.0, adequacy + 0.3)   # a second independent search ran
    if finder and finder.get("ok") and finder.get("index_hits", 0) >= 5:
        adequacy = min(1.0, adequacy + 0.1)   # structured-index leg reviewed real papers

    conn = rw()
    conn.execute("""
        INSERT INTO novelty_audits
        (claim_id, verdict, confidence, citations, explanation, novel_residue,
         model, web_used, created_at, corroborated, finder_model, finder_found,
         search_adequacy)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
        (claim["id"], verdict, conf, json.dumps(citations)[:4000],
         str(parsed.get("explanation", ""))[:2000],
         (str(residue)[:1000] if residue else None),
         MODEL, time.time(), corroborated,
         (FINDER_MODEL if finder and finder.get("ok") else None),
         finder_found, adequacy))
    conn.execute("""
        UPDATE knowledge_claims
        SET prior_work_status = ?, prior_work_citation = COALESCE(?, prior_work_citation),
            is_empirical_fact = ?
        WHERE id = ?""",
        ("LIT_" + verdict, first_ref or None, 1 if fact else 0, claim["id"]))
    conn.commit()
    conn.close()
    return verdict, conf, residue


def audit_one(claim, supports, key):
    """Do the network audit for one claim and commit its verdict. Pure per-claim
    unit — no shared connection (write_verdict opens its own short-lived rw conn),
    so this is safe to run concurrently across a thread pool. Returns a result dict."""
    try:
        content, usage = call_llm(build_prompt(claim, supports), key)
        parsed = extract_json(content)
        if not parsed:
            raise ValueError(f"no JSON in response: {content[:200]!r}")
        # burden flip: only spend a finder call on the claims that would otherwise
        # land on the shelf as "novel" — the NOT_FOUND / PARTIALLY_KNOWN ones.
        primary = str(parsed.get("verdict", "")).strip().upper()
        finder = None
        if primary in ("NOT_FOUND", "PARTIALLY_KNOWN"):
            finder = find_counterpart(claim, supports, key)
        verdict, conf, residue = write_verdict(claim, parsed, finder)
        return {"claim": claim, "ok": True, "verdict": verdict, "conf": conf,
                "residue": residue,
                "refuted_by_finder": bool(finder and finder.get("found")),
                "corroborated": bool(finder and finder.get("ok") and not finder.get("found")),
                "tok_in": usage.get("prompt_tokens", 0) or 0,
                "tok_out": usage.get("completion_tokens", 0) or 0}
    except Exception as e:  # noqa: BLE001 — one bad audit must not sink the batch
        return {"claim": claim, "ok": False, "error": str(e),
                "tok_in": 0, "tok_out": 0}


def main():
    ap = argparse.ArgumentParser(description="Literature-check promoted claims")
    ap.add_argument("--status", default="ESTABLISHED")
    ap.add_argument("--limit", type=int, default=25,
                    help="Cap per run (safety for the unattended cron; raise for a manual backlog drain)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="Parallel audits in flight (default 1 = serial, unchanged for the "
                         "unattended cron). Each audit is an independent OpenRouter+web call "
                         "with no DB held, so a manual backlog drain can safely run 8-12 at "
                         "once and finish 283 claims in ~15 min instead of days.")
    ap.add_argument("--time-budget", type=float, default=0.0,
                    help="Stop launching new audits after this many seconds (0=unlimited). "
                         "The unattended cron script timeout is 120s and each audit is ~26s, "
                         "so a run with --limit 25 would be KILLED mid-call; pass e.g. 100 so "
                         "the run exits cleanly having committed what it finished.")
    ap.add_argument("--claim", type=int, default=None, help="Audit one claim id")
    ap.add_argument("--force", action="store_true",
                    help="Re-audit claims that already have a novelty_audits row")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ensure_table()
    targets = get_targets(args.status, args.limit, args.claim, args.force)
    if not targets:
        print("No eligible claims (all audited? try --force).")
        return 0
    conc = max(1, args.concurrency)
    print(f"{len(targets)} claim(s) to audit against the literature "
          f"[{MODEL} + web plugin]" + (f"  (concurrency={conc})" if conc > 1 else "") + "\n")
    if args.dry_run:
        for claim, _ in targets:
            text = (claim["claim_summary"] or claim["hypothesis_text"] or "")[:100]
            print(f"  #{claim['id']:6d} wsc={claim['wsc']:.1f} [{claim['domain']}] {text}")
        print("\n(dry run — no calls)")
        return 0

    key = get_key()
    tally = {v: 0 for v in VERDICTS}
    failures = 0
    tok_in = tok_out = 0
    total = len(targets)
    t_start = time.time()
    done_n = 0

    def harvest(res):
        """Fold one completed audit into the running tallies + print a line."""
        nonlocal failures, tok_in, tok_out, done_n
        done_n += 1
        tok_in += res["tok_in"]
        tok_out += res["tok_out"]
        cid = res["claim"]["id"]
        label = (res["claim"]["claim_summary"] or res["claim"]["hypothesis_text"] or "")[:80]
        if res["ok"]:
            tally[res["verdict"]] += 1
            line = f"[{done_n}/{total}] #{cid} -> {res['verdict']}"
            if res["conf"] is not None:
                line += f" ({res['conf']:.2f})"
            if res.get("refuted_by_finder"):
                line += "  [finder found the paper — novelty refuted]"
            elif res.get("corroborated"):
                line += "  [absence corroborated by independent finder]"
            if res["residue"]:
                line += f"  residue: {str(res['residue'])[:80]}"
            print(line)
        else:
            failures += 1
            print(f"[{done_n}/{total}] #{cid} !! FAILED: {res['error']}  {label}")

    if conc == 1:
        # serial path — byte-for-byte the original cron behavior (paced by SLEEP_BETWEEN)
        for i, (claim, supports) in enumerate(targets, 1):
            if args.time_budget and (time.time() - t_start) > args.time_budget:
                print(f"[time-budget {args.time_budget:.0f}s reached after {i-1} audits — "
                      f"exiting cleanly; remaining claims picked up next run]")
                break
            harvest(audit_one(claim, supports, key))
            time.sleep(SLEEP_BETWEEN)
    else:
        # bounded concurrent path — keep exactly `conc` audits in flight; stop
        # submitting once the time budget is spent (in-flight ones finish cleanly).
        it = iter(targets)
        pending = set()

        def submit_next():
            if args.time_budget and (time.time() - t_start) > args.time_budget:
                return False
            try:
                claim, supports = next(it)
            except StopIteration:
                return False
            pending.add(ex.submit(audit_one, claim, supports, key))
            return True

        with ThreadPoolExecutor(max_workers=conc) as ex:
            while len(pending) < conc and submit_next():
                pass
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in completed:
                    harvest(fut.result())
                    submit_next()
        if args.time_budget and (time.time() - t_start) > args.time_budget and done_n < total:
            print(f"[time-budget {args.time_budget:.0f}s reached — {done_n}/{total} done, "
                  f"remaining picked up next run]")

    print("\n=== verdicts ===")
    for v in VERDICTS:
        print(f"  {v:16s} {tally[v]}")
    if failures:
        print(f"  (failed: {failures} — re-run to retry; audited claims are skipped)")
    est = tok_in / 1e6 * 0.09 + tok_out / 1e6 * 0.18
    print(f"tokens: {tok_in} in / {tok_out} out  (~${est:.2f} model cost "
          f"+ web plugin per-search fees)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
