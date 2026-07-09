#!/usr/bin/env python3
"""discovery_report.py — the Discoveries plate.

The third editorial page (with the two topology plates): WHAT HAS THE SYSTEM
ACTUALLY DISCOVERED. Renders the discovery_candidates ledger (discovery_spotlight,
hourly) with each claim's full evidence trail — cleaned finding, tier, support,
retests, adversarial record, mapped scope (claim_scopes), literature verdict +
novel residue (novelty_audits), artifacts pointer — as a self-contained HTML
document. Read-only against prometheus.db.

Design contract (operator standard, see topology_report.py):
  - zero external requests: system fonts, inline CSS, NO JS at all on this page
  - turquoise plate: page ground #40e0d0, panels lighter turquoise (never white),
    ink #04302b, accent deep sea-teal, failure deep coral, no green
  - editorial: serif headings, hairline rules, numbered sections, method footnotes

Output: ~/.hermes/docs/prometheus-discoveries.html (canonical) + ~/prometheus-discoveries.html
Usage:  python3 discovery_report.py            # build both outputs
"""
import html
import os
import re
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mechanism_calibration import classify_mechanism
import discovery_routing as routing

HERMES = os.path.expanduser("~/.hermes")
DB = os.path.join(HERMES, "prometheus.db")
CANONICAL = os.path.join(HERMES, "docs", "prometheus-discoveries.html")
MIRROR = os.path.expanduser("~/prometheus-discoveries.html")
FULL_CARDS = 24          # entries rendered as full cards; the rest go to the ledger table
KILL_CARDS = 6           # gauntlet section: terminal knockouts rendered as full cards

# Contribution-type tag, mapped from the mechanism-shape classifier. Display
# hint only, and confident-only: OTHER renders no tag (a missing tag beats a
# wrong one — the classifier is a first-match regex over prose).
_SHAPE_LABEL = {
    "TRANSFER": "cross-domain transfer",
    "THRESHOLD": "threshold / onset",
    "PHASE": "regime behavior",
    "SCALING": "scaling law",
    "CONSERVATION": "invariance",
    "OPTIMUM": "optimum / saturation",
    "EQUIVALENCE": "equivalence",
    "MONOTONIC": "quantitative relationship",
}


def _esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _ro():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------
# finding text — strip orchestration/provenance markers for display.
# hypothesis_text/claim_summary are NEVER modified in the DB (identity rule);
# this cleaning is display-only.
# --------------------------------------------------------------------------

_LEAD_MARKERS = [
    re.compile(r"^\s*(?:ATTACK_OUTCOME|ARBITRATION_VERDICT)\s*:\s*[A-Z_]+\s*[.:—-]?\s*", re.I),
    re.compile(r"^\s*(?:CONFIRMED|SUPPORTED|PARTIALLY[ _]SUPPORTED|VERIFIED|VALIDATED)\s*"
               r"(?:\(NARROWED\)|\(boundary mapped\))?\s*[.:—-]\s*", re.I),
    re.compile(r"^\s*DISCOVERY HARDENING[^.:]*[.:]\s*", re.I),
    re.compile(r"^\s*DISPUTE ARBITRATION FOR CLAIM \d+\s*[.:]\s*", re.I),
    re.compile(r"^\s*(?:DISCRIMINATING |INDEPENDENT )?TEST(?:ED)?\s*:\s*", re.I),
    re.compile(r"^\s*\[[A-Z0-9 _-]+(?:from [^\]]+)?\]\s*", re.I),   # task tags
]
_INLINE_MARKERS = [
    re.compile(r"\s*(?:ADVERSARIAL_REPLICATION_FOR_CLAIM|DISPUTE_ARBITRATION_FOR_CLAIM)\s*:\s*\d+\.?", re.I),
    re.compile(r"\s*CURIOSITY_ID\s*:\s*\d+\.?", re.I),
]


def clean_finding(summary, hypothesis):
    text = (summary or "").strip() or (hypothesis or "").strip()
    for _ in range(6):                       # markers stack; strip repeatedly
        before = text
        for rx in _LEAD_MARKERS:
            text = rx.sub("", text, count=1)
        if text == before:
            break
    for rx in _INLINE_MARKERS:
        text = rx.sub("", text)
    text = text.strip()
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text or (hypothesis or "").strip()


def _flow(text):
    """Collapse whitespace for display; never truncates."""
    return " ".join((text or "").split())


# --------------------------------------------------------------------------
# data assembly (read-only)
# --------------------------------------------------------------------------

def gather(conn):
    """Assemble the shelf, then route every candidate: only the `discovery` route
    is ranked as a discovery. The routing runs at render time (not just in the
    spotlight ledger) so a ledger row written before the router shipped is still
    filtered correctly — the page can never show a claim the router would bin."""
    rows = conn.execute("""
        SELECT dc.claim_id, dc.discovery_score, dc.novelty_confidence, dc.status AS ledger_status,
               dc.survivals, dc.hardening_task_id,
               kc.claim_status AS tier, kc.claim_summary, kc.hypothesis_text, kc.domain,
               COALESCE(kc.weighted_support_count,0) AS wsc,
               COALESCE(kc.n_independent_retests,0) AS retests,
               COALESCE(kc.n_formal_replications,0) AS formal,
               kc.prior_work_citation, kc.first_experiment_id,
               COALESCE(kc.is_empirical_fact,0) AS is_fact
        FROM discovery_candidates dc
        JOIN knowledge_claims kc ON kc.id = dc.claim_id
        WHERE dc.status != 'broken'
          AND kc.claim_status IN ('REPLICATED','ESTABLISHED')
          AND kc.prior_work_status = 'LIT_NOT_FOUND'
          AND COALESCE(kc.is_meta,0) = 0
        ORDER BY (dc.status='hardened') DESC, dc.discovery_score DESC
    """).fetchall()

    shelf, bins = [], {routing.KNOWN_IN_LIT: [], routing.EMPIRICAL_FACT: [],
                       routing.DERIVABLE: []}
    for r in rows:
        cid = r["claim_id"]
        atk = dict(conn.execute(
            "SELECT status, COUNT(*) FROM adversarial_replications "
            "WHERE claim_id=? GROUP BY status", (cid,)).fetchall())
        attackers = [a[0].split("/")[-1].split(":")[0] for a in conn.execute(
            "SELECT DISTINCT attacker_model FROM adversarial_replications "
            "WHERE claim_id=? AND attacker_model IS NOT NULL", (cid,)).fetchall()]
        scope = conn.execute(
            "SELECT scope_text FROM claim_scopes WHERE claim_id=? "
            "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        aud = conn.execute(
            "SELECT novel_residue, confidence, explanation, citations FROM novelty_audits "
            "WHERE claim_id=? AND verdict='NOT_FOUND' ORDER BY id DESC LIMIT 1",
            (cid,)).fetchone()
        adv_prose = [a[0] for a in conn.execute(
            "SELECT (SELECT result FROM experiments e WHERE e.id=ar.experiment_id) "
            "FROM adversarial_replications ar WHERE ar.claim_id=? ORDER BY ar.id DESC LIMIT 4",
            (cid,)).fetchall() if a[0]]
        task = conn.execute(
            "SELECT kanban_task_id FROM experiments WHERE id=? LIMIT 1",
            (r["first_experiment_id"],)).fetchone()
        finding = clean_finding(r["claim_summary"], r["hypothesis_text"])
        scope_txt = scope["scope_text"] if scope else None
        residue = (aud["novel_residue"] or "").strip() if aud else ""
        citation = (r["prior_work_citation"] or "").strip()

        route, reason = routing.route_claim(
            summary=r["claim_summary"] or "", hypothesis=r["hypothesis_text"] or "",
            residue=residue, explanation=(aud["explanation"] if aud else "") or "",
            citations=(aud["citations"] if aud else "") or "", scope=scope_txt or "",
            adversarial_texts=adv_prose, prior_work_citation=citation,
            is_empirical_fact=bool(r["is_fact"]), novelty_confidence=r["novelty_confidence"])

        entry = {
            "id": cid,
            "shape": _SHAPE_LABEL.get(classify_mechanism(finding)),
            "score": r["discovery_score"],
            "nconf": r["novelty_confidence"],
            "ledger": r["ledger_status"],
            "tier": r["tier"],
            "finding": finding,
            "domain": (r["domain"] or "uncategorized").replace("_", " "),
            "wsc": r["wsc"], "retests": r["retests"], "formal": r["formal"],
            "surv": atk.get("survived", 0),
            "narrowed": atk.get("narrowed", 0),
            "broken": atk.get("refuted", 0) + atk.get("refuted_arbitrated", 0),
            "attackers": sorted(set(attackers)),
            "scope": scope_txt,
            "residue": residue,
            "citation": citation,
            "route": route, "route_reason": reason,
            "drift": routing.central_quantity_drift(finding, residue, scope_txt),
            "sim_flag": routing.simulation_flag(residue, r["claim_summary"] or "", scope_txt or ""),
            "artifacts": f"~/.hermes/artifacts/{task['kanban_task_id']}/"
                         if task and task["kanban_task_id"] else None,
        }
        if route == routing.DISCOVERY:
            shelf.append(entry)
        elif route in bins:
            bins[route].append(entry)
        # search_miss falls through to the §"search miss" table below via ledger status

    miss = conn.execute("""
        SELECT dc.claim_id, dc.discovery_score, kc.claim_summary, kc.hypothesis_text, kc.domain
        FROM discovery_candidates dc JOIN knowledge_claims kc ON kc.id=dc.claim_id
        WHERE dc.status='likely_search_miss'
        ORDER BY dc.discovery_score DESC
    """).fetchall()

    return shelf, miss, bins


def gather_gauntlet(conn):
    """The filter's other output: terminal knockouts + the kill instruments.
    A claim counts as knocked out when it REACHED the promotion band
    (claim_status_history) and now sits below it — not DISPUTED, which is
    transient and self-clearing, but demoted after settlement."""
    g = {}
    g["fell"] = conn.execute("""
        SELECT COUNT(DISTINCT h.claim_id) FROM claim_status_history h
        JOIN knowledge_claims kc ON kc.id = h.claim_id
        WHERE h.old_status IN ('REPLICATED','ESTABLISHED')
          AND kc.claim_status NOT IN ('REPLICATED','ESTABLISHED','DISPUTED')
          AND COALESCE(kc.is_meta,0)=0""").fetchone()[0]
    g["broken_attacks"] = conn.execute(
        "SELECT COUNT(*) FROM adversarial_replications "
        "WHERE status IN ('refuted','refuted_arbitrated')").fetchone()[0]
    g["retracted"] = conn.execute(
        "SELECT COUNT(*) FROM claim_evidence "
        "WHERE evidence_type='retracted_by_arbitration'").fetchone()[0]
    g["retest_disagreed"] = conn.execute(
        "SELECT COUNT(*) FROM replication_results "
        "WHERE replication_status IN ('disagreed','disagreed_arbitrated')").fetchone()[0]
    arb = dict(conn.execute(
        "SELECT status, COUNT(*) FROM dispute_arbitrations "
        "WHERE status IN ('resolved_a','resolved_b','both_wrong','regime_split') "
        "GROUP BY status").fetchall())
    g["arb_original"] = arb.get("resolved_a", 0)
    g["arb_challenger"] = arb.get("resolved_b", 0)
    g["arb_both_wrong"] = arb.get("both_wrong", 0)
    g["arb_regime"] = arb.get("regime_split", 0)

    rows = conn.execute("""
        SELECT kc.id, kc.claim_status AS now_status, kc.domain, kc.hypothesis_text,
               (SELECT COUNT(*) FROM claim_status_history h2
                WHERE h2.claim_id=kc.id AND h2.old_status='ESTABLISHED') AS was_est
        FROM knowledge_claims kc
        WHERE COALESCE(kc.is_meta,0)=0
          AND kc.claim_status NOT IN ('REPLICATED','ESTABLISHED','DISPUTED')
          AND EXISTS (SELECT 1 FROM claim_status_history h
                      WHERE h.claim_id=kc.id
                        AND h.old_status IN ('REPLICATED','ESTABLISHED'))
          AND (EXISTS (SELECT 1 FROM adversarial_replications ar
                       WHERE ar.claim_id=kc.id
                         AND ar.status IN ('refuted','refuted_arbitrated'))
               OR EXISTS (SELECT 1 FROM dispute_arbitrations da
                          WHERE da.claim_id=kc.id
                            AND da.status IN ('resolved_b','both_wrong')))
        ORDER BY was_est DESC, kc.id DESC LIMIT ?""", (KILL_CARDS,)).fetchall()

    kills = []
    for r in rows:
        instrument, finding = None, ""
        atk = conn.execute("""
            SELECT ar.experiment_id, ar.attacker_model,
                   (SELECT e.result FROM experiments e WHERE e.id=ar.experiment_id) AS res
            FROM adversarial_replications ar
            WHERE ar.claim_id=? AND ar.status IN ('refuted','refuted_arbitrated')
            ORDER BY ar.id DESC LIMIT 1""", (r["id"],)).fetchone()
        if atk and (atk["res"] or "").strip():
            fam = (atk["attacker_model"] or "").split("/")[-1].split(":")[0]
            instrument = ("independent adversarial attack"
                          + (f" ({fam})" if fam else ""))
            finding = atk["res"]
        else:
            arb_row = conn.execute("""
                SELECT da.status,
                       (SELECT e.result FROM experiments e WHERE e.id=da.experiment_id) AS res
                FROM dispute_arbitrations da
                WHERE da.claim_id=? AND da.status IN ('resolved_b','both_wrong')
                ORDER BY da.id DESC LIMIT 1""", (r["id"],)).fetchone()
            if arb_row:
                instrument = ("decisive arbitration — challenger correct"
                              if arb_row["status"] == "resolved_b"
                              else "decisive arbitration — both sides failed the test")
                finding = arb_row["res"] or ""
        if not instrument:
            continue
        kills.append({
            "id": r["id"],
            "was": "ESTABLISHED" if r["was_est"] else "REPLICATED",
            "now": r["now_status"] or "RETIRED",
            "domain": (r["domain"] or "uncategorized").replace("_", " "),
            "claimed": clean_finding(r["hypothesis_text"], ""),
            "instrument": instrument,
            "kill_finding": clean_finding(finding, ""),
        })
    g["kills"] = kills
    return g


def gather_stats(conn):
    stats = {
        "scopes": conn.execute("SELECT COUNT(*) FROM claim_scopes").fetchone()[0],
        "established": conn.execute(
            "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='ESTABLISHED' "
            "AND COALESCE(is_meta,0)=0 AND COALESCE(is_empirical_fact,0)=0").fetchone()[0],
        "replicated": conn.execute(
            "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='REPLICATED' "
            "AND COALESCE(is_meta,0)=0 AND COALESCE(is_empirical_fact,0)=0").fetchone()[0],
        "survived": conn.execute(
            "SELECT COUNT(*) FROM adversarial_replications WHERE status='survived'").fetchone()[0],
    }
    return stats


# --------------------------------------------------------------------------
# style — same plate as the topology pages, plus discovery-card idioms
# --------------------------------------------------------------------------

_CSS = r"""
:root{
  --paper:#40e0d0; --panel:#5fe6d9; --panel2:#4de2d4;
  --ink:#04302b; --ink2:#12524a; --faint:#22625a;
  --rule:rgba(4,48,43,.30); --rule-lt:rgba(4,48,43,.14);
  --accent:#025c52; --accent-ink:#02463f; --good:#046d61; --bad:#c23a26;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Charter,Georgia,serif;
  --sans:system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  color-scheme: light;
}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{background:var(--paper);color:var(--ink);font-family:var(--serif);
  line-height:1.55;font-size:16px;padding-bottom:72px}
a{color:var(--accent-ink)}
.wrap{max-width:1080px;margin:0 auto;padding:0 28px}
.measure{max-width:76ch}
sup.fn{font-size:.62em;line-height:0}
sup.fn a{color:var(--accent);text-decoration:none;font-family:var(--sans)}
.masthead{padding:44px 0 0}
.masthead .rule-heavy{border-top:2.5px solid var(--ink);border-bottom:1px solid var(--ink);
  height:6px;margin-bottom:26px}
.kicker{font-family:var(--sans);font-size:.68rem;letter-spacing:.24em;text-transform:uppercase;
  color:var(--accent);font-weight:600;margin-bottom:10px}
h1{font-size:2.75rem;font-weight:600;letter-spacing:-.015em;line-height:1.08;max-width:22ch}
.dek{font-style:italic;font-size:1.12rem;color:var(--ink2);margin-top:14px;max-width:62ch}
.byline{display:flex;flex-wrap:wrap;gap:8px 26px;font-family:var(--mono);font-size:.7rem;
  color:var(--ink2);margin-top:20px;padding:12px 0 14px;border-top:1px solid var(--rule);
  border-bottom:1px solid var(--rule)}
.byline b{color:var(--ink);font-weight:500}
.abstract{margin:34px 0 8px;font-size:1.04rem}
.abstract p{margin-bottom:.9em;text-align:justify;hyphens:auto}
.slug{font-family:var(--mono);font-size:.88em;color:var(--ink)}
.abstract p:first-child::first-letter{font-size:3.05em;float:left;line-height:.82;
  padding:.06em .08em 0 0;color:var(--accent);font-weight:600}
.vitals{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--rule);
  background:var(--panel);margin:30px 0 8px}
.vital{padding:16px 18px 14px;border-right:1px solid var(--rule-lt);border-bottom:1px solid var(--rule-lt)}
.vital:nth-child(4n){border-right:0}
.vital:nth-last-child(-n+4){border-bottom:0}
.vital .v{font-size:1.72rem;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.vital .l{font-family:var(--sans);font-size:.6rem;letter-spacing:.14em;text-transform:uppercase;
  color:var(--ink2);margin-top:2px}
.vital .s{font-family:var(--mono);font-size:.66rem;color:var(--faint);margin-top:4px}
@media(max-width:900px){.vitals{grid-template-columns:repeat(2,1fr)}
  .vital:nth-child(2n){border-right:0}.vital:nth-last-child(-n+2){border-bottom:0}}
section{margin:46px 0}
h2{font-size:1.28rem;font-weight:600;display:flex;align-items:baseline;gap:12px;
  padding-bottom:8px;border-bottom:1px solid var(--rule);margin-bottom:18px}
h2 .no{font-family:var(--sans);font-size:.78rem;color:var(--accent);font-weight:700;letter-spacing:.06em}
.sectionintro{color:var(--ink2);font-size:.92rem;margin:-6px 0 16px;max-width:80ch}

/* discovery cards */
.disc{border:1px solid var(--rule);background:var(--panel);margin:0 0 14px;
  page-break-inside:avoid}
.disc .hd{display:flex;align-items:baseline;gap:12px;padding:12px 16px 0}
.disc .idx{font-family:var(--sans);font-weight:700;color:var(--accent);font-size:.8rem;min-width:2.2ch}
.disc .score{margin-left:auto;font-family:var(--mono);font-size:.72rem;color:var(--ink2);
  white-space:nowrap}
.disc .score b{color:var(--ink);font-size:.9rem}
.badge{font-family:var(--sans);font-size:.58rem;letter-spacing:.11em;text-transform:uppercase;
  border:1px solid var(--rule);padding:2px 8px;white-space:nowrap;color:var(--ink2)}
.badge.est{background:var(--accent);border-color:var(--accent);color:#dff7f2}
.badge.hardening{border-style:dashed}
.badge.hardened{background:#033f38;border-color:#033f38;color:#dff7f2}
.disc .finding{padding:8px 16px 4px;font-size:1.03rem;line-height:1.5;max-width:88ch}
.disc .meta{display:flex;flex-wrap:wrap;gap:4px 22px;padding:8px 16px 12px;
  font-family:var(--mono);font-size:.68rem;color:var(--ink2)}
.disc .meta b{color:var(--ink);font-weight:500}
.disc .meta .bad{color:var(--bad)}
.subblock{margin:0 16px 12px;padding:10px 12px;background:var(--panel2);
  border-left:2px solid var(--accent)}
.subblock .lbl{font-family:var(--sans);font-size:.58rem;letter-spacing:.13em;
  text-transform:uppercase;color:var(--accent-ink);font-weight:700;margin-bottom:3px}
.subblock .tx{font-size:.86rem;color:var(--ink2);line-height:1.45}
.disc .ft{display:flex;flex-wrap:wrap;gap:4px 22px;padding:8px 16px 12px;
  border-top:1px solid var(--rule-lt);font-family:var(--mono);font-size:.64rem;color:var(--faint)}

/* gauntlet — the filter's kills */
.disc.kill{border-left:3px solid var(--bad)}
.badge.kill{background:var(--bad);border-color:var(--bad);color:#ffe9e4}
.badge.was{border-style:dashed}
.subblock.bad{border-left-color:var(--bad)}
.subblock.bad .lbl{color:var(--bad)}

/* ledger table */
.tbl{border:1px solid var(--rule);background:var(--panel);margin-bottom:10px;overflow-x:auto}
.tbl .cap{font-family:var(--sans);font-size:.64rem;letter-spacing:.13em;text-transform:uppercase;
  color:var(--ink2);padding:10px 14px;border-bottom:1px solid var(--rule-lt);background:var(--panel2)}
table{width:100%;border-collapse:collapse;font-family:var(--sans);font-size:.76rem}
th{font-size:.6rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink2);
  text-align:left;padding:8px 10px;border-bottom:1px solid var(--rule)}
td{padding:7px 10px;border-bottom:1px solid var(--rule-lt);vertical-align:top}
td.num{text-align:right;font-family:var(--mono);font-size:.72rem;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
figcaption{font-size:.8rem;color:var(--ink2);padding:10px 14px;border-top:1px solid var(--rule-lt);
  font-style:italic}

/* footnotes + colophon */
.footnotes{margin-top:54px;border-top:2px solid var(--ink);padding-top:14px}
.footnotes h3{font-family:var(--sans);font-size:.68rem;letter-spacing:.2em;
  text-transform:uppercase;color:var(--ink2);margin-bottom:10px}
.footnotes ol{margin-left:18px;font-size:.84rem;color:var(--ink2)}
.footnotes li{margin-bottom:7px;line-height:1.5}
.colophon{margin-top:34px;padding-top:12px;border-top:1px solid var(--rule);
  font-family:var(--mono);font-size:.66rem;color:var(--faint);line-height:1.7}
"""


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def _badge(entry):
    b = []
    if entry["tier"] == "ESTABLISHED":
        b.append('<span class="badge est">Established</span>')
    else:
        b.append('<span class="badge">Replicated</span>')
    if entry["ledger"] == "hardened":
        b.append('<span class="badge hardened">Hardened</span>')
    elif entry["ledger"] == "hardening":
        b.append('<span class="badge hardening">Re-derivation in flight</span>')
    if entry.get("shape"):
        b.append(f'<span class="badge">{_esc(entry["shape"])}</span>')
    return "".join(b)


def _kill_card(i, k):
    return (
        f'<article class="disc kill">'
        f'<div class="hd"><span class="idx">K{i}</span>'
        f'<span class="badge kill">Knocked out</span>'
        f'<span class="badge was">was {_esc(k["was"].title())}</span>'
        f'<span class="score">now <b>{_esc((k["now"] or "").title())}</b></span></div>'
        f'<div class="finding">{_esc(_flow(k["claimed"]))}</div>'
        f'<div class="subblock bad"><div class="lbl">Killed by — {_esc(k["instrument"])}</div>'
        f'<div class="tx">{_esc(_flow(k["kill_finding"]))}</div></div>'
        f'<div class="ft"><span>claim #{k["id"]}</span>'
        f'<span>domain {_esc(k["domain"])}</span></div>'
        f'</article>')


def _card(i, e):
    meta = [
        f"<span>domain <b>{_esc(e['domain'])}</b></span>",
        f"<span>support <b>{e['wsc']:.1f}</b> wsc</span>",
        f"<span>retests <b>{e['retests']}</b> · formal <b>{e['formal']}</b></span>",
        f"<span>attacks <b>{e['surv']}</b> survived · <b>{e['narrowed']}</b> narrowed"
        + (f" · <b class=\"bad\">{e['broken']}</b> broken" if e['broken'] else "") + "</span>",
    ]
    if e["attackers"]:
        meta.append(f"<span>attacked by <b>{_esc(', '.join(e['attackers']))}</b></span>")
    if e["nconf"] is not None:
        meta.append(f"<span>novelty (weak prior) <b>{e['nconf']:.2f}</b></span>")
    if e["drift"]:
        h, r, d = e["drift"]
        meta.append(f'<span class="bad">headline/residue mismatch: {h} vs {r}</span>')

    blocks = []
    if e["scope"]:
        blocks.append(
            f'<div class="subblock"><div class="lbl">Where it holds — mapped scope</div>'
            f'<div class="tx">{_esc(_flow(clean_finding(e["scope"], "")))}</div></div>')
    if e["sim_flag"]:
        blocks.append(
            f'<div class="subblock"><div class="lbl">Model-internal — numbers are parameter-dependent</div>'
            f'<div class="tx">{_esc(e["sim_flag"])}; the values are properties of a self-designed '
            f'simulation, not measured from the world. Ranked on robustness, not novelty.</div></div>')
    lit = ("No counterpart surfaced by a single-pass web search (model-judged — "
           "absence of evidence, not established novelty)"
           + (f"; novel residue: {_esc(_flow(e['residue']))}" if e["residue"] else "")
           + ".") if not e["citation"] else f"Closest prior work: {_esc(_flow(e['citation']))}."
    blocks.append(
        f'<div class="subblock"><div class="lbl">Literature</div>'
        f'<div class="tx">{lit}</div></div>')

    ft = [f"<span>claim #{e['id']}</span>"]
    if e["artifacts"]:
        ft.append(f"<span>code + data: {_esc(e['artifacts'])}</span>")

    return (
        f'<article class="disc">'
        f'<div class="hd"><span class="idx">{i:02d}</span>{_badge(e)}'
        f'<span class="score">discovery score <b>{e["score"]:.1f}</b>/100</span></div>'
        f'<div class="finding">{_esc(_flow(e["finding"]))}</div>'
        f'<div class="meta">{"".join(meta)}</div>'
        f'{"".join(blocks)}'
        f'<div class="ft">{"".join(ft)}</div>'
        f'</article>')


def _ledger_rows(entries, start):
    rows = []
    for i, e in enumerate(entries, start=start):
        rows.append(
            f"<tr><td class=\"num\">{i:02d}</td>"
            f"<td>{_esc(_flow(e['finding']))}</td>"
            f"<td>{_esc(e['domain'])}</td>"
            f"<td>{_esc(e['tier'].title())}{' · hardening' if e['ledger']=='hardening' else ''}</td>"
            f"<td class=\"num\">{e['surv']}/{e['narrowed']}</td>"
            f"<td class=\"num\">{e['score']:.0f}</td></tr>")
    return "".join(rows)


_BIN_META = {
    routing.KNOWN_IN_LIT: ("Known — prior work identified",
        "A publication identifier (PMID / DOI / arXiv) or a recorded prior-work citation "
        "appears in the claim’s own evidence. It survived the system’s attacks — but it is "
        "not a first documentation of anything, because the paper it would be first to "
        "document is cited inside it. These are rediscoveries, correctly labeled."),
    routing.EMPIRICAL_FACT: ("Empirical-fact lookup",
        "A recall or verification of an established published value or event — physical "
        "constants, catalog figures, dated announcements. Real and checkable, but a lookup, "
        "not a discovery. Footnote 3 always promised these were excluded; now they are."),
    routing.DERIVABLE: ("Derivable — analytic or definitional",
        "The claim’s own prose calls it a mathematical identity, an analytically proven "
        "result, or a property of the representation (‘geometric property of TF-IDF vector "
        "space’). A theorem that survives an adversarial attack is still a theorem; "
        "robustness of a tautology is just the tautology. Routed here, off the discovery shelf."),
}


def _bin_row(e):
    return (f"<tr><td class=\"num\">#{e['id']}</td>"
            f"<td>{_esc(_flow(e['finding']))}</td>"
            f"<td>{_esc(e['domain'])}</td>"
            f"<td class=\"num\">{e['score']:.0f}</td>"
            f"<td>{_esc(_flow(e['route_reason']))}</td></tr>")


def _binned_section(bins):
    total = sum(len(v) for v in bins.values())
    if not total:
        return ""
    tables = []
    for route in (routing.KNOWN_IN_LIT, routing.EMPIRICAL_FACT, routing.DERIVABLE):
        items = sorted(bins.get(route, []), key=lambda e: -(e["score"] or 0))
        if not items:
            continue
        title, blurb = _BIN_META[route]
        rows = "".join(_bin_row(e) for e in items)
        tables.append(
            f'<div class="tbl" style="margin-top:14px"><div class="cap">{_esc(title)} — '
            f'{len(items)} claim{"s" if len(items)!=1 else ""}</div>'
            f'<figcaption style="border-top:0;border-bottom:1px solid var(--rule-lt)">{_esc(blurb)}</figcaption>'
            f'<table><thead><tr><th>claim</th><th>finding</th><th>domain</th>'
            f'<th>had score</th><th>routed here because</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')
    return (
        '<section><h2><span class="no">§2</span>What the router moved off the shelf</h2>'
        '<p class="sectionintro">A survivors-only shelf hides its own errors. These '
        f'{total} claims passed replication and attack — they are robust — but they are not '
        'discoveries: each names its own prior work, is an empirical lookup, or is analytically '
        'derivable. The signal was already in the cards; the router now reads it instead of '
        'ranking past it. Shown with the reason each was moved, so the filter is auditable.</p>'
        + "".join(tables) + '</section>')


def gather_near_duplicates(conn):
    """High-confidence near-DUPLICATE promotion-band pairs from claim_similarity's
    `claim_near_duplicates` ledger (the between-claim redundancy pass). Only the
    'duplicate' tier, only where BOTH claims are still on the science shelf. Empty
    when the ledger table does not exist yet (fresh DB / cron not run, or the embed
    server is mid-swap) — the section is then omitted."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='claim_near_duplicates'").fetchone():
        return []
    return conn.execute("""
        SELECT d.claim_a, d.claim_b, d.similarity, d.tier,
               a.claim_status AS sa, b.claim_status AS sb,
               a.claim_summary AS suma, a.hypothesis_text AS hypa,
               b.claim_summary AS sumb, b.hypothesis_text AS hypb
        FROM claim_near_duplicates d
        JOIN knowledge_claims a ON a.id = d.claim_a
        JOIN knowledge_claims b ON b.id = d.claim_b
        WHERE d.tier IN ('duplicate','related')
          AND a.claim_status IN ('REPLICATED','ESTABLISHED')
          AND b.claim_status IN ('REPLICATED','ESTABLISHED')
          AND COALESCE(a.is_empirical_fact,0)=0 AND COALESCE(a.is_meta,0)=0
          AND COALESCE(b.is_empirical_fact,0)=0 AND COALESCE(b.is_meta,0)=0
        ORDER BY (a.claim_status='ESTABLISHED' AND b.claim_status='ESTABLISHED') DESC,
                 d.similarity DESC""").fetchall()


def _near_dup_section(neardups):
    if not neardups:
        return ""
    try:
        from claim_similarity import DUP_THRESHOLD as _dup_thr, RELATED_THRESHOLD as _rel_thr
    except Exception:
        _dup_thr, _rel_thr = 0.78, 0.72
    dups = [d for d in neardups if d["tier"] == "duplicate"]
    related = [d for d in neardups if d["tier"] == "related"]
    if not dups and not related:
        return ""
    rows = []
    for d in dups:
        both_est = d["sa"] == "ESTABLISHED" and d["sb"] == "ESTABLISHED"
        star = ' <span class="slug">both established</span>' if both_est else ""
        fa = _flow(clean_finding(d["suma"], d["hypa"]))
        fb = _flow(clean_finding(d["sumb"], d["hypb"]))
        rows.append(
            f'<tr><td class="num">#{d["claim_a"]} / #{d["claim_b"]}</td>'
            f'<td class="num">{d["similarity"]:.2f}</td>'
            f'<td>{_esc(d["sa"][:4])}/{_esc(d["sb"][:4])}{star}</td>'
            f'<td>{_esc(fa)}<br><span style="opacity:.65">{_esc(fb)}</span></td></tr>')
    dup_block = (
        '<div class="tbl"><table><thead><tr><th>claims</th><th>cosine</th><th>tiers</th>'
        f'<th>the finding, both ways</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        if rows else '<p class="sectionintro">No exact-duplicate pairs this pass.</p>')
    related_block = ""
    if related:
        ids = ", ".join(f"#{d['claim_a']}/#{d['claim_b']}" for d in related[:20])
        related_block = (
            f'<p class="sectionintro" style="margin-top:10px">A further <b>{len(related)}</b> '
            f'pair{"s" if len(related) != 1 else ""} embed as strongly related '
            f'(cosine ≥ {_rel_thr:g}) — <em>cross-link</em> rather than merge candidates, often the '
            f'same mechanism on a distinct question: {ids}{"…" if len(related) > 20 else ""}. '
            f'Full set in the <span class="slug">claim_near_duplicates</span> ledger.</p>')
    return (
        '<section><h2><span class="no">§4</span>Near-duplicate claims</h2>'
        '<p class="sectionintro">A claim’s identity is its question, so the same finding '
        'asked two ways mints two claims — and both can promote and sit on the shelf as one '
        f'discovery double-counted. These pairs embed as near-identical (cosine ≥ {_dup_thr:g}). They are '
        'flagged for merge or cross-link, <em>not</em> auto-merged: two duplicates can legitimately '
        'hold different verdicts, which is itself worth seeing.</p>'
        + dup_block + related_block + '</section>')


def render(entries, miss, stats, gauntlet, bins, neardups=()):
    gen_at = time.strftime("%Y-%m-%d %H:%M")
    n_moved = sum(len(v) for v in bins.values())
    n_est = sum(1 for e in entries if e["tier"] == "ESTABLISHED")
    n_hardening = sum(1 for e in entries if e["ledger"] == "hardening")
    n_hardened = sum(1 for e in entries if e["ledger"] == "hardened")

    vitals = "".join([
        f'<div class="vital"><div class="v">{len(entries)}</div><div class="l">on the shelf</div>'
        f'<div class="s">passed all 3 gates · ranked by robustness</div></div>',
        f'<div class="vital"><div class="v">{n_moved}</div><div class="l">moved off the shelf</div>'
        f'<div class="s">known · empirical · derivable (§2)</div></div>',
        f'<div class="vital"><div class="v">{n_est}</div><div class="l">established</div>'
        f'<div class="s">survived an adversarial attack</div></div>',
        f'<div class="vital"><div class="v">{stats["scopes"]}</div><div class="l">regimes mapped</div>'
        f'<div class="s">claim_scopes — where claims hold</div></div>',
    ])

    abstract = (
        '<p>Every entry below is a claim this system produced by running real experiments, that '
        'then <span class="slug">survived its own machinery’s attempts to kill it</span>: independent '
        'retests, adversarial attacks by other model families, false-consensus checks, circularity '
        'review. That establishes one thing well — <em>robustness</em>. It does not by itself '
        'establish the two things the word “discovery” also needs: that the result is <em>new to the '
        'world</em>, and that it is <em>non-trivial</em> (a contingent fact, not a theorem, a '
        'definition, or a lookup). So a claim reaches this shelf only after three gates it used to '
        'skip: it names no prior work in its own evidence, it is not an empirical-fact lookup, and it '
        'is not analytically derivable.<sup class="fn"><a href="#fn1">1</a></sup></p>'
        '<p>The shelf is <span class="slug">ranked by robustness</span>, not by literature-absence — '
        'a single web search returning “not found” is a weak prior and is credited as one, never as '
        'the headline pillar it used to be.<sup class="fn"><a href="#fn2">2</a></sup> The claims the '
        'router moved off the shelf are shown in §2 with the reason for each, because a survivors-only '
        'page hides its own errors. Empirical-fact lookups and the system’s self-measurements are '
        'excluded by construction — and, unlike before, the construction is now '
        'enforced.<sup class="fn"><a href="#fn3">3</a></sup></p>')

    binned_html = _binned_section(bins)
    neardup_html = _near_dup_section(neardups)
    cards = "".join(_card(i, e) for i, e in enumerate(entries[:FULL_CARDS], start=1))
    rest = entries[FULL_CARDS:]
    tbl = ""
    if rest:
        tbl = (
            f'<div class="tbl"><div class="cap">The rest of the shelf — ranked {FULL_CARDS + 1}–{len(entries)}</div>'
            f'<table><thead><tr><th>#</th><th>finding</th><th>domain</th><th>tier</th>'
            f'<th>atk s/n</th><th>score</th></tr></thead>'
            f'<tbody>{_ledger_rows(rest, FULL_CARDS + 1)}</tbody></table></div>')

    kills = gauntlet.get("kills", [])
    kill_vitals = "".join([
        f'<div class="vital"><div class="v">{gauntlet["fell"]}</div><div class="l">knocked out of the band</div>'
        f'<div class="s">reached replicated+, later demoted</div></div>',
        f'<div class="vital"><div class="v">{gauntlet["broken_attacks"]}</div><div class="l">attacks broke their target</div>'
        f'<div class="s">adversarial replications, cross-family</div></div>',
        f'<div class="vital"><div class="v">{gauntlet["retracted"]}</div><div class="l">evidence rows retracted</div>'
        f'<div class="s">by decisive arbitration</div></div>',
        f'<div class="vital"><div class="v">{gauntlet["arb_challenger"]}<small> vs {gauntlet["arb_original"]}</small></div>'
        f'<div class="l">challenger vs original</div>'
        f'<div class="s">arbitration verdicts (+{gauntlet["arb_both_wrong"]} both wrong, '
        f'{gauntlet["arb_regime"]} regime splits)</div></div>',
    ])
    gauntlet_html = (
        '<section><h2><span class="no">§3</span>The gauntlet at work</h2>'
        '<p class="sectionintro">A survivors-only shelf is indistinguishable from a system with no '
        'filter — so here is the filter’s other output. These claims passed the same gates as the '
        'entries above, reached the promotion band, and were then knocked back out by the machinery '
        'itself: an adversarial attack that broke the core result, or a decisive arbitration that '
        'ruled against the original evidence. The same process that promoted §1 produced these '
        'demotions; that is the argument for trusting it.</p>'
        f'<div class="vitals">{kill_vitals}</div>'
        + "".join(_kill_card(i, k) for i, k in enumerate(kills, start=1))
        + f'<p class="sectionintro" style="margin-top:10px">{gauntlet["retest_disagreed"]} independent '
        'retests disagreed with their original experiment across the claim base — each one either '
        'settled by arbitration or standing as a live dispute.</p>'
        '</section>')

    miss_html = ""
    if miss:
        rows = "".join(
            f"<tr><td class=\"num\">#{m['claim_id']}</td>"
            f"<td>{_esc(_flow(clean_finding(m['claim_summary'], m['hypothesis_text'])))}</td>"
            f"<td>{_esc((m['domain'] or '').replace('_', ' '))}</td></tr>" for m in miss)
        miss_html = (
            '<section><h2><span class="no">§5</span>Flagged: likely search misses</h2>'
            '<p class="sectionintro">The literature audit reported “not found” for these, but with '
            'confidence too low to trust — the more likely explanation is a search miss, not novelty. '
            'They are queued for re-audit rather than for experiments, and are kept off the shelf above.</p>'
            f'<div class="tbl"><table><thead><tr><th>claim</th><th>finding</th><th>domain</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></section>')

    footnotes = (
        '<div class="footnotes"><h3>Method</h3><ol>'
        '<li id="fn1">Gates every entry passed: intake quality gate (validate_quality ≥ 40) → claim '
        'replication (independent retest required) → spurious-agreement &lt; 0.6 (supports must agree in '
        'substance, not just vote) → circularity review → answer-level adjudication → for ESTABLISHED, '
        'a survived adversarial attack, usually by a different model family than the one that produced '
        'the evidence. The literature check is the weakest gate and the page treats it that way: it is '
        'ONE web-search pass judged by a cross-family model, so “not found” means absence from a single '
        'search — a far weaker fact than absence from the literature. Low-confidence passes are flagged '
        'as likely search misses (§3) instead of shelved. None of these gates makes a claim true; '
        'together they make “confidently wrong” expensive.</li>'
        '<li id="fn2">Robustness score, 0–100: 45 × validation tier (ESTABLISHED = 1.0, REPLICATED = '
        '0.6) + 25 × adversarial break-survivals (capped at 4) + 15 × support depth (log-scaled wsc) '
        '+ 15 × novelty as a <em>weak prior</em> (literature-absence confidence, discounted while it '
        'rests on a single un-corroborated search, and zeroed for model-internal simulation numbers). '
        'The old score gave literature-absence 40 of 100 points and had no term at all for triviality; '
        'that is why a media-saturated paper and a textbook identity could rank first and second. '
        'Novelty is now a <em>gate</em> (§1 intro) far more than a score term. Interpretable by '
        'design — no learned weights.</li>'
        '<li id="fn3">Excluded by construction — and, as of this build, actually enforced in the '
        'candidate queries and re-checked by the router at render, not merely in the summary counts: '
        '<span class="slug">is_meta</span> claims (the system measuring itself) and '
        '<span class="slug">is_empirical_fact</span> claims. The empirical-fact test was broadened '
        'beyond a whitelist of named instruments to the <em>shape</em> of a lookup — physical/'
        'astronomical constants, CODATA/NIST reference values, catalog figures, dated announcements — '
        'and any claim whose own evidence carries a PMID / DOI / arXiv id is treated as a rediscovery '
        '(§2), because a first documentation cannot cite the paper it claims to precede.</li>'
        '<li>A claim marked <em>re-derivation in flight</em> has a DISCOVERY-HARDENING attack running: '
        'an independent method must reproduce the result from scratch. Survival hardens it; a break '
        'demotes it off this page. Mapped-scope blocks come from boundary-mapping experiments spawned '
        'when an attack NARROWED the claim — honest boundaries, printed with the finding.</li>'
        '</ol></div>')

    colophon = (
        '<div class="colophon">'
        f'generated {_esc(gen_at)} · discovery_report.py, read-only over prometheus.db · '
        'refreshed hourly after discovery_spotlight · self-contained document, no external requests, no scripts<br>'
        'companion plates: <a href="prometheus-topology.html">the topology of inquiry</a> · '
        '<a href="prometheus-topology-3d.html">the topology in three dimensions</a>'
        '</div>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prometheus · Discoveries</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="masthead">
    <div class="rule-heavy"></div>
    <div class="kicker">Prometheus · Discovery Shelf</div>
    <h1>What the System Found</h1>
    <p class="dek">Robust claims that also cleared three gates the word “discovery” requires —
    no prior work named in their own evidence, not an empirical lookup, not analytically
    derivable. Ranked by robustness, not by literature-absence; shown together with the claims
    the router moved off the shelf, and the ones the same machinery knocked out.</p>
    <div class="byline">
      <span>generated <b>{_esc(gen_at)}</b></span>
      <span><b>{len(entries)}</b> candidates on the shelf</span>
      <span><b>{n_est}</b> established</span>
      <span><b>{stats['survived']}</b> survived attacks (all claims)</span>
      <span><b>{stats['established']}</b>/<b>{stats['replicated']}</b> discovery-shelf tiers E/R</span>
    </div>
  </header>

  <div class="abstract measure">{abstract}</div>

  <div class="vitals">{vitals}</div>

  <section>
    <h2><span class="no">§1</span>The shelf</h2>
    <p class="sectionintro">Ranked by robustness.<sup class="fn"><a href="#fn2">2</a></sup> The top {min(FULL_CARDS, len(entries))} carry their
    full evidence trail; the remainder are listed in the ledger below them.</p>
    {cards}
    {tbl}
  </section>

  {binned_html}

  {gauntlet_html}

  {neardup_html}

  {miss_html}

  {footnotes}
  {colophon}
</div>
</body>
</html>"""


def main():
    conn = _ro()
    entries, miss, bins = gather(conn)
    stats = gather_stats(conn)
    gauntlet = gather_gauntlet(conn)
    neardups = gather_near_duplicates(conn)
    conn.close()
    page = render(entries, miss, stats, gauntlet, bins, neardups)
    n_moved = sum(len(v) for v in bins.values())
    for path in (CANONICAL, MIRROR):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(page)
        os.replace(tmp, path)
        print(f"wrote {path} ({len(page)//1024} KB, {len(entries)} on shelf, "
              f"{n_moved} moved off, {len(miss)} search-miss)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
