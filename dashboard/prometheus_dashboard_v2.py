#!/usr/bin/env python3
"""
Prometheus Dashboard v3 — the turquoise plate, live.

Rewrite of prometheus_dashboard_v2.py onto the operator's editorial design
contract (see docs/prometheus-topology.html and the discoveries plate):

  * zero external requests — system fonts, hand-rolled SSR SVG charts,
    ~60 lines of vanilla JS (nav + auto-refresh). Chart.js CDN is GONE.
  * tone-on-tone turquoise: page ground #40e0d0, panels #5fe6d9/#4de2d4,
    sea-ink text, deep coral for failures, no green anywhere.
  * editorial structure: serif headings, hairline rules, numbered figures.

New visibility (subsystems added since v2 was written):
  gates (retest/attack/arbitration/contradiction/cross-domain),
  independence (prior-feed stamps + self-arming haircut),
  discovery pipeline (candidates ledger + routes),
  novelty audits (literature verdicts + finder overrides + residues),
  meta-prober (self-prediction reconciliation),
  claim scopes & boundary pool.

Serving contract:
  * ALL sqlite opens are mode=ro URIs — this process can never hold a
    write lock on the live WAL databases.
  * one full-page build cached for CACHE_TTL seconds behind a lock —
    a page build costs seconds against the big DBs; repeat hits are free.
  * every section gatherer is fault-isolated: a broken query renders a
    coral "section unavailable" note instead of a 500.
"""

import http.server
import socketserver
import sqlite3
import json
import os
import re
import time
import html
import math
import threading
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")
KANBAN_DB_PATH = os.path.expanduser("~/.hermes/kanban.db")
RAG_DB_PATH = os.path.expanduser("~/.hermes/rag/rag.db")
CRON_JOBS_PATH = os.path.expanduser("~/.hermes/cron/jobs.json")
TOPOLOGY_EXPORT = os.path.expanduser("~/.hermes/topology_full_export.json")
ARMED_PATH = os.path.expanduser("~/.hermes/independence_armed.json")
WORLD_CAL_PATH = os.path.expanduser("~/.hermes/world_calibration.json")
WORLD_GATE_PATH = os.path.expanduser("~/.hermes/world_gate_armed.json")
MECH_CAL_PATH = os.path.expanduser("~/.hermes/mechanism_calibration.json")
META_CAL_PATH = os.path.expanduser("~/.hermes/meta_transfer_calibration.json")
NOVELTY_CAL_PATH = os.path.expanduser("~/.hermes/novelty_calibration.json")
DISC_CAND_PATH = os.path.expanduser("~/.hermes/discovery_candidates.json")
A1_OFF_PATH = os.path.expanduser("~/.hermes/a1_router.OFF")
BACKUPS_DIR = os.path.expanduser("~/.hermes/backups")
LOGS_DIR = os.path.expanduser("~/.hermes/logs")
DOCS_DIR = os.path.expanduser("~/.hermes/docs")
WM_URL = "http://127.0.0.1:19876"
A1_URL = "http://127.0.0.1:8001"
PORT = 8889
CACHE_TTL = 25  # seconds; matches the default 30s auto-refresh

# ── the turquoise plate ─────────────────────────────────────────────
INK = "#04302b"
INK2 = "#12524a"
FAINT = "#22625a"
ACCENT = "#025c52"
SIGNAL = "#046d61"
SIGNAL_DK = "#033f38"
BAD = "#c23a26"
PANEL2 = "#4de2d4"

# mineral data tones (mid-dark, desaturated; no green, no cyan)
WINE = "#8d3b4a"
SLATE = "#3f5d8c"
OCHRE = "#b0783a"
NAVY = "#2e4369"
MAUVE = "#7d5a78"
TERRA = "#a45238"
PLUM = "#5d3a56"
STEEL = "#4a5d74"
MINERALS = [WINE, SLATE, OCHRE, NAVY, MAUVE, TERRA, PLUM, STEEL]

VERDICT_COLORS = {
    "confirmed": SIGNAL_DK,
    "supported": SLATE,
    "partial": OCHRE,
    "refuted": BAD,
}


# ═════════════════════════════════════════════════════════════════
# helpers
# ═════════════════════════════════════════════════════════════════

def _esc(s):
    if s is None:
        return ""
    return html.escape(str(s))


def _safe_float(v, default=0.0):
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _fmt_age(secs):
    if secs is None:
        return "—"
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs/60:.1f}m"
    if secs < 86400:
        return f"{secs/3600:.1f}h"
    return f"{secs/86400:.1f}d"


def _fmt_ts(ts):
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(ts)[:8]


def _fmt_num(n):
    if n is None:
        return "0"
    try:
        return f"{int(n):,}"
    except (ValueError, TypeError):
        return str(n)


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def get_db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    return conn


def q1(db, sql, params=(), default=0):
    """Scalar query with a default."""
    try:
        row = db.execute(sql, params).fetchone()
        if row is None:
            return default
        v = row[0]
        return default if v is None else v
    except sqlite3.Error:
        return default


def query_rag(sql, params=()):
    try:
        conn = sqlite3.connect(f"file:{RAG_DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def query_kanban(sql, params=()):
    """Read-only kanban.db query (dispatch state: fleet mix, churn, retest yield)."""
    try:
        conn = sqlite3.connect(f"file:{KANBAN_DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def daemon_query(endpoint, data=None):
    try:
        req = urllib.request.Request(
            f"{WM_URL}{endpoint}", data=json.dumps(data or {}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


# ═════════════════════════════════════════════════════════════════
# data gatherers — ported from v2 (verbatim SQL) + new subsystems
# ═════════════════════════════════════════════════════════════════

def get_stats():
    db = get_db()
    now = time.time()
    s = {
        "exp_count": q1(db, "SELECT COUNT(*) FROM experiments WHERE id LIKE 'exp_%'"),
        "completed": q1(db, "SELECT COUNT(*) FROM experiments WHERE status='completed'"),
        "running": q1(db, "SELECT COUNT(*) FROM experiments WHERE status='running'"),
        "pending": q1(db, "SELECT COUNT(*) FROM experiments WHERE status='pending'"),
        "domain_count": q1(db, "SELECT COUNT(*) FROM domains"),
        "skill_count": q1(db, "SELECT COUNT(*) FROM skills"),
        "cur_active": q1(db, "SELECT COUNT(*) FROM curiosities WHERE status='active'"),
        "cur_resolved": q1(db, "SELECT COUNT(*) FROM curiosities WHERE status='resolved'"),
        "wr_count": q1(db, "SELECT COUNT(*) FROM worker_results"),
        "tt_count": q1(db, "SELECT COUNT(*) FROM transfer_tracking"),
        "kc_count": q1(db, "SELECT COUNT(*) FROM knowledge_claims"),
        "exp_last_hour": q1(db, "SELECT COUNT(*) FROM experiments WHERE created_at >= ?", (now - 3600,)),
        "exp_last_5min": q1(db, "SELECT COUNT(*) FROM experiments WHERE created_at >= ?", (now - 300,)),
        "exp_24h": q1(db, "SELECT COUNT(*) FROM experiments WHERE created_at >= ?", (now - 86400,)),
    }
    # worker liveness lives in kanban task_events (kind='heartbeat'), NOT the
    # legacy prometheus.db heartbeats table — that table died 2026-06-03 when
    # liveness moved to the dispatcher, and this tile silently read 0 for a month.
    # Definition: RUNNING tasks that heartbeated in the last 120s (cadence ~67s).
    # A bare distinct-heartbeat count over 5m rolled up completed-and-replaced
    # tasks and read 25-29 against the ~20-concurrent dispatcher cap.
    hb_rows = query_kanban("""
        SELECT COUNT(*) w,
               (SELECT COUNT(*) FROM tasks WHERE status='running') running
        FROM tasks t
        WHERE t.status='running' AND EXISTS (
          SELECT 1 FROM task_events e WHERE e.task_id = t.id
            AND e.kind='heartbeat' AND e.created_at >= strftime('%s','now') - 120)""")
    s["active_workers"] = (hb_rows[0].get("w") or 0) if hb_rows else 0
    s["running_tasks"] = (hb_rows[0].get("running") or 0) if hb_rows else 0
    v = db.execute("""
        SELECT
          SUM(CASE WHEN UPPER(result) LIKE 'CONFIRMED%' AND UPPER(result) NOT LIKE 'PARTIALLY%' THEN 1 ELSE 0 END) AS confirmed,
          SUM(CASE WHEN UPPER(result) LIKE 'SUPPORTED%' AND UPPER(result) NOT LIKE 'PARTIALLY%' THEN 1 ELSE 0 END) AS supported,
          SUM(CASE WHEN UPPER(result) LIKE 'PARTIALLY%' THEN 1 ELSE 0 END) AS partial,
          SUM(CASE WHEN UPPER(result) LIKE 'REFUTED%' THEN 1 ELSE 0 END) AS refuted,
          COUNT(*) AS total
        FROM experiments WHERE status='completed' AND result IS NOT NULL
    """).fetchone()
    s["verdicts"] = dict(v) if v else {}
    tiers = db.execute("""
        SELECT claim_status, COUNT(*) c FROM knowledge_claims
        WHERE claim_status IS NOT NULL GROUP BY claim_status
    """).fetchall()
    s["tiers"] = {r["claim_status"]: r["c"] for r in tiers}
    # science-tier candidates (the headline convention: exclude meta + lookup)
    s["candidates_sci"] = q1(db, """
        SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='CANDIDATE'
        AND COALESCE(is_meta,0)=0 AND COALESCE(is_empirical_fact,0)=0""")
    # discovery-shelf tiers exclude meta + empirical-fact claims
    s["replicated_shelf"] = q1(db, """
        SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='REPLICATED'
        AND COALESCE(is_meta,0)=0 AND COALESCE(is_empirical_fact,0)=0""")
    s["established_shelf"] = q1(db, """
        SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='ESTABLISHED'
        AND COALESCE(is_meta,0)=0 AND COALESCE(is_empirical_fact,0)=0""")
    topo = None
    try:
        if os.path.exists(TOPOLOGY_EXPORT):
            with open(TOPOLOGY_EXPORT) as f:
                t = json.load(f)
            gm = t.get("graph_metrics", {})
            topo = {"nodes": gm.get("num_nodes", 0), "edges": gm.get("num_directed_edges", 0),
                    "communities": len(t.get("communities", []))}
    except Exception:
        topo = None
    s["topology"] = topo
    db.close()
    return s


def get_series():
    """Hourly (24h) and daily (14d) experiment counts with verdict split — SQL only."""
    db = get_db()
    now = time.time()
    hours = []
    rows = db.execute("""
        SELECT CAST(created_at/3600 AS INTEGER)*3600 AS bucket,
          COUNT(*) AS total,
          SUM(CASE WHEN UPPER(result) LIKE 'REFUTED%' THEN 1 ELSE 0 END) AS refuted
        FROM experiments WHERE created_at >= ? GROUP BY bucket ORDER BY bucket
    """, (now - 86400,)).fetchall()
    by_bucket = {r["bucket"]: r for r in rows}
    start = int(now // 3600) * 3600 - 23 * 3600
    for i in range(24):
        b = start + i * 3600
        r = by_bucket.get(b)
        hours.append({
            "label": datetime.fromtimestamp(b).strftime("%H"),
            "total": r["total"] if r else 0,
            "refuted": (r["refuted"] or 0) if r else 0,
        })
    days = []
    rows = db.execute("""
        SELECT CAST(created_at/86400 AS INTEGER)*86400 AS bucket, COUNT(*) AS total
        FROM experiments WHERE created_at >= ? GROUP BY bucket ORDER BY bucket
    """, (now - 14 * 86400,)).fetchall()
    by_day = {r["bucket"]: r["total"] for r in rows}
    dstart = int(now // 86400) * 86400 - 13 * 86400
    for i in range(14):
        b = dstart + i * 86400
        days.append({"label": datetime.fromtimestamp(b).strftime("%m/%d"),
                     "total": by_day.get(b, 0)})
    db.close()
    return {"hours": hours, "days": days}


def get_feed(limit=40):
    db = get_db()
    rows = db.execute("""
        SELECT id, domain, status, confidence_change, result,
               substr(hypothesis, 1, 220) AS hyp, created_at
        FROM experiments WHERE id LIKE 'exp_%'
        ORDER BY created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    out = []
    for r in rows:
        res = (r["result"] or "").upper()
        verdict = ""
        if res.startswith("PARTIALLY"):
            verdict = "partial"
        elif res.startswith("CONFIRMED"):
            verdict = "confirmed"
        elif res.startswith("SUPPORTED"):
            verdict = "supported"
        elif res.startswith("REFUTED"):
            verdict = "refuted"
        out.append({
            "time": _fmt_ts(r["created_at"]),
            "domain": (r["domain"] or "").replace("_", " "),
            "status": r["status"] or "",
            "cc": _safe_float(r["confidence_change"]),
            "hyp": r["hyp"] or "",
            "verdict": verdict,
        })
    db.close()
    return out


def get_gates():
    """The epistemic ladder — every gate a claim passes through."""
    db = get_db()
    now = time.time()
    g = {}
    # replication (retest) results
    repl = db.execute("SELECT replication_status, COUNT(*) c FROM replication_results GROUP BY replication_status").fetchall()
    g["replication"] = {r["replication_status"] or "?": r["c"] for r in repl}
    g["retested_claims"] = q1(db, "SELECT COUNT(*) FROM knowledge_claims WHERE COALESCE(n_independent_retests,0) > 0")
    # adversarial attacks — explicit buckets ('pending' once counted 235 terminal
    # refuted_arbitrated rows as in-flight)
    g["attacks"] = {
        "survived": q1(db, "SELECT COUNT(*) FROM adversarial_replications WHERE status='survived'"),
        "refuted": q1(db, "SELECT COUNT(*) FROM adversarial_replications WHERE status='refuted'"),
        "refuted_arbitrated": q1(db, "SELECT COUNT(*) FROM adversarial_replications WHERE status='refuted_arbitrated'"),
        "narrowed": q1(db, "SELECT COUNT(*) FROM adversarial_replications WHERE status='narrowed'"),
        "expired": q1(db, "SELECT COUNT(*) FROM adversarial_replications WHERE status='expired'"),
        "pending": q1(db, "SELECT COUNT(*) FROM adversarial_replications WHERE status='pending'"),
    }
    sv, rf = g["attacks"]["survived"], g["attacks"]["refuted"]
    g["attack_survival_pct"] = _pct(sv, sv + rf)
    # attacker-pool mix (cross-family: who actually attacks, and how claims fare)
    g["attackers"] = [dict(r) for r in db.execute("""
        SELECT COALESCE(attacker_model,'(legacy/mimo)') model, COUNT(*) n,
               SUM(status='survived') sv, SUM(status IN ('refuted','refuted_arbitrated')) rf
        FROM adversarial_replications GROUP BY 1 ORDER BY n DESC LIMIT 8""").fetchall()]
    g["attackers_48h"] = [dict(r) for r in db.execute("""
        SELECT COALESCE(attacker_model,'(legacy/mimo)') model, COUNT(*) n
        FROM adversarial_replications WHERE created_at > ? GROUP BY 1 ORDER BY n DESC
        """, (now - 172800,)).fetchall()]
    # NARROWED terminal cap — attack-saturated claims (>=5 narrows, 0 survivals)
    g["attack_saturated"] = q1(db, """
        SELECT COUNT(*) FROM (
          SELECT claim_id FROM adversarial_replications GROUP BY claim_id
          HAVING SUM(status='narrowed') >= 5 AND SUM(status='survived') = 0)""")
    # method-code alignment — the 5th gate (ScientistOne I4): described method != code
    g["method_code"] = {
        "flagged": q1(db, "SELECT COUNT(*) FROM knowledge_claims WHERE method_code_mismatch=1"),
        "aligned": q1(db, "SELECT COUNT(*) FROM knowledge_claims WHERE method_code_mismatch=0"),
        "reviews": q1(db, "SELECT COUNT(*) FROM method_code_reviews"),
    }
    g["method_code_flagged_rows"] = [dict(r) for r in db.execute("""
        SELECT kc.id, kc.claim_status,
               substr(COALESCE(mr.reason,''),1,150) reason
        FROM knowledge_claims kc
        LEFT JOIN method_code_reviews mr ON mr.claim_id = kc.id AND mr.mismatch=1
        WHERE kc.method_code_mismatch=1 GROUP BY kc.id LIMIT 6""").fetchall()]
    # circularity cap — self-fulfilling constructions (capped at CANDIDATE)
    g["circular_flagged"] = q1(db, "SELECT COUNT(*) FROM knowledge_claims WHERE circular_construction=1")
    # arbitration of DISPUTED claims
    arb = db.execute("SELECT status, COUNT(*) c FROM dispute_arbitrations GROUP BY status").fetchall()
    g["arbitration"] = {r["status"] or "?": r["c"] for r in arb}
    g["arb_24h"] = q1(db, "SELECT COUNT(*) FROM dispute_arbitrations WHERE created_at >= ?", (now - 86400,))
    g["disputed_now"] = q1(db, "SELECT COUNT(*) FROM knowledge_claims WHERE claim_status='DISPUTED'")
    # contradiction attacks (literature CONTRADICTED -> re-examine)
    g["contradiction"] = {
        "total": q1(db, "SELECT COUNT(*) FROM contradiction_attacks"),
        "enqueued": q1(db, "SELECT COUNT(*) FROM contradiction_attacks WHERE status='enqueued'"),
    }
    # cross-domain disconfirmation gate
    g["xdomain"] = {
        "total": q1(db, "SELECT COUNT(*) FROM xdomain_disconfirm_checks"),
        "enqueued": q1(db, "SELECT COUNT(*) FROM xdomain_disconfirm_checks WHERE status='enqueued'"),
    }
    # spurious agreement (false consensus)
    sa = db.execute("""
        SELECT ROUND(AVG(spurious_agreement),3) avg_sa,
               SUM(CASE WHEN spurious_agreement >= 0.6 THEN 1 ELSE 0 END) high_sa,
               SUM(CASE WHEN spurious_agreement IS NOT NULL THEN 1 ELSE 0 END) has_sa
        FROM knowledge_claims
    """).fetchone()
    g["sa"] = dict(sa) if sa else {}
    g["sa_high_promotion"] = q1(db, """
        SELECT COUNT(*) FROM knowledge_claims
        WHERE spurious_agreement >= 0.6 AND posterior >= 0.6""")
    # transfer survival — the actual product. Survival is a REPLICATION outcome
    # (replication_results), not a transfer_tracking status: the old query looked
    # for 'replicated'/'disagreed' rows in transfer_tracking (zero of each exist)
    # and rendered a flagship 0.0% forever. transfer_tracking supplies VOLUME.
    ts_row = db.execute("""
        SELECT SUM(CASE WHEN replication_status='replicated' THEN 1 ELSE 0 END) rep,
               SUM(CASE WHEN replication_status='disagreed' THEN 1 ELSE 0 END) dis
        FROM replication_results
    """).fetchone()  # 'disagreed' only — arbitration-settled rows are excluded, matching health_snapshot
    rep, dis = (ts_row["rep"] or 0), (ts_row["dis"] or 0)
    xdom_done = q1(db, "SELECT COUNT(*) FROM transfer_tracking WHERE status='completed_cross_domain'")
    g["transfer"] = {"replicated": rep, "disagreed": dis,
                     "total": xdom_done,
                     "survival_pct": _pct(rep, rep + dis)}
    # retest credit yield — the standing GATES tripwire (healthy 85-100%).
    # Ports health_snapshot's fixed definition: [RE-EXAMINE]/[ADVERSARIAL] tasks
    # that merely QUOTE a retest summary are excluded (they can never credit),
    # and credited/creditable share one clock (the completed-task set, 24h).
    try:
        kb = sqlite3.connect(f"file:{KANBAN_DB_PATH}?mode=ro", uri=True, timeout=5)
        kb.execute("PRAGMA busy_timeout=3000")
        kb.row_factory = sqlite3.Row
        kb.execute(f"ATTACH DATABASE 'file:{DB_PATH}?mode=ro' AS p")
        # tag must sit in the title HEAD (<=40 chars in): quote-colliders — tasks
        # that merely QUOTE a '[CANDIDATE-RETEST] ...' string ([RE-EXAMINE]/
        # [ADVERSARIAL] cards, REFUTED-branch "would flip"/"boundary conditions"
        # follow-ups) — embed the tag deep and can never earn a credit
        _base = ("((INSTR(title,'[CANDIDATE-RETEST]') BETWEEN 1 AND 40) "
                 "OR (INSTR(title,'[RETEST-GATE]') BETWEEN 1 AND 40)) "
                 "AND title NOT LIKE '%[RE-EXAMINE]%' AND title NOT LIKE '%[ADVERSARIAL]%' "
                 "AND status IN ('done','archived','completed') "
                 "AND completed_at > strftime('%s','now')-86400")
        _done = q1(kb, f"SELECT COUNT(*) FROM tasks WHERE {_base}")
        _credited = q1(kb, f"""
            SELECT COUNT(*) FROM tasks WHERE {_base}
              AND EXISTS (SELECT 1 FROM p.replication_results r2
                          WHERE r2.validation_task_id = tasks.id)""")
        # duplicate completions can never earn a credit (replication_results is
        # UNIQUE per source experiment) — exclude them from the denominator,
        # matching health_snapshot, or the tile cries wolf at small n
        _dups = q1(kb, f"""
            SELECT COUNT(*) FROM tasks t
            JOIN p.curiosities c
              ON c.id = CAST(SUBSTR(t.body, INSTR(t.body,'CURIOSITY_ID:')+13) AS INTEGER)
            JOIN p.replication_results rr ON rr.original_experiment_id = c.source_experiment
            WHERE ((INSTR(t.title,'[CANDIDATE-RETEST]') BETWEEN 1 AND 40)
                   OR (INSTR(t.title,'[RETEST-GATE]') BETWEEN 1 AND 40))
              AND t.title NOT LIKE '%[RE-EXAMINE]%' AND t.title NOT LIKE '%[ADVERSARIAL]%'
              AND t.status IN ('done','archived','completed')
              AND t.completed_at > strftime('%s','now')-86400
              AND t.body LIKE '%CURIOSITY_ID:%'
              AND NOT EXISTS (SELECT 1 FROM p.replication_results r2
                              WHERE r2.validation_task_id = t.id)""")
        kb.close()
        _creditable = max(_done - (_dups or 0), 0)
        g["retest_yield"] = {"done": _creditable, "credited": _credited,
                             "pct": _pct(_credited, _creditable) if _creditable else None}
    except Exception:
        g["retest_yield"] = {"done": 0, "credited": 0, "pct": None}
    # scoped re-attack convergence — PER CLAIM (raw attack totals mix pre-cap
    # orbit debris + cross-domain probes whose material narrows are the map
    # growing, so they sit near parity even when healthy). A claim converges
    # when its LATEST post-scope outcome is survived.
    conv = db.execute("""
        WITH scoped AS (
          SELECT ar.claim_id, ar.status, ar.created_at
          FROM adversarial_replications ar
          JOIN (SELECT claim_id, MIN(created_at) t0 FROM claim_scopes GROUP BY claim_id) s
            ON s.claim_id = ar.claim_id AND ar.created_at > s.t0
          WHERE ar.status='narrowed' OR ar.status LIKE 'survived%'),
        latest AS (
          SELECT claim_id,
                 (SELECT status FROM scoped s2 WHERE s2.claim_id = scoped.claim_id
                  ORDER BY created_at DESC LIMIT 1) last_status,
                 SUM(status='narrowed') n_narrow,
                 SUM(status LIKE 'survived%') n_surv
          FROM scoped GROUP BY claim_id)
        SELECT COUNT(*) total, SUM(last_status LIKE 'survived%') conv,
               SUM(last_status='narrowed' AND n_narrow < 3) mapping,
               SUM(n_narrow >= 3 AND n_surv = 0) orbiting
        FROM latest""").fetchone()
    g["scoped_conv"] = {"total": (conv["total"] or 0) if conv else 0,
                        "converged": (conv["conv"] or 0) if conv else 0,
                        "mapping": (conv["mapping"] or 0) if conv else 0,
                        "orbiting": (conv["orbiting"] or 0) if conv else 0}
    db.close()
    return g


def get_independence():
    """Prior-feed stamps + the self-arming haircut."""
    armed = {}
    try:
        with open(ARMED_PATH) as f:
            armed = json.load(f)
    except Exception:
        armed = {}
    db = get_db()
    now = time.time()
    stamps_total = q1(db, "SELECT COUNT(*) FROM task_prior_feed")
    stamps_fed = q1(db, "SELECT COUNT(*) FROM task_prior_feed WHERE prior_fed=1")
    stamps_24h = q1(db, "SELECT COUNT(*) FROM task_prior_feed WHERE created_at >= ?", (now - 86400,))
    ci_n = q1(db, "SELECT COUNT(*) FROM claim_independence")
    ci_avg = q1(db, "SELECT ROUND(AVG(independence_multiplier),3) FROM claim_independence", default=None)
    ci_bitten = q1(db, "SELECT COUNT(*) FROM claim_independence WHERE independence_multiplier < 1.0")
    mono = q1(db, "SELECT COUNT(*) FROM claim_independence WHERE single_family=1")
    buckets = db.execute("""
        SELECT
          SUM(CASE WHEN independence_multiplier >= 0.999 THEN 1 ELSE 0 END) b100,
          SUM(CASE WHEN independence_multiplier >= 0.85 AND independence_multiplier < 0.999 THEN 1 ELSE 0 END) b85,
          SUM(CASE WHEN independence_multiplier >= 0.65 AND independence_multiplier < 0.85 THEN 1 ELSE 0 END) b65,
          SUM(CASE WHEN independence_multiplier < 0.65 THEN 1 ELSE 0 END) b50
        FROM claim_independence
    """).fetchone()
    db.close()
    return {
        "armed": armed,
        "stamps_total": stamps_total, "stamps_fed": stamps_fed,
        "stamps_24h": stamps_24h,
        "ci_n": ci_n, "ci_avg": ci_avg, "ci_bitten": ci_bitten,
        "mono": mono, "mono_pct": _pct(mono, ci_n),
        "mult_buckets": [
            {"label": "1.0 (blind)", "value": buckets["b100"] or 0},
            {"label": ".85–1", "value": buckets["b85"] or 0},
            {"label": ".65–.85", "value": buckets["b65"] or 0},
            {"label": ".5–.65 (all-fed)", "value": buckets["b50"] or 0},
        ],
    }


def get_discovery():
    """The discovery pipeline — candidates ledger, routes, top of the shelf."""
    db = get_db()
    routes = db.execute("""
        SELECT COALESCE(route,'(legacy)') route, COALESCE(status,'?') status, COUNT(*) c
        FROM discovery_candidates GROUP BY route, status ORDER BY c DESC
    """).fetchall()
    route_rows = [dict(r) for r in routes]
    top = db.execute("""
        SELECT dc.claim_id, dc.discovery_score, dc.novelty_confidence, dc.tier,
               dc.survivals, dc.status, dc.route,
               COALESCE(kc.claim_summary, substr(kc.hypothesis_text,1,180)) AS text
        FROM discovery_candidates dc
        LEFT JOIN knowledge_claims kc ON kc.id = dc.claim_id
        WHERE COALESCE(dc.route,'discovery')='discovery'
        ORDER BY dc.discovery_score DESC LIMIT 8
    """).fetchall()
    top_rows = [dict(r) for r in top]
    knocked = db.execute("""
        SELECT COALESCE(route,'?') route, COUNT(*) c FROM discovery_candidates
        WHERE status='off_shelf' GROUP BY route
    """).fetchall()
    # THE shelf = the spotlight's fresh-routing pass (discovery_candidates.json,
    # rewritten hourly). The DB ledger above is CUMULATIVE — rows routed before a
    # router change linger there, so it can show claims the live router has since
    # binned. Fresh JSON is authoritative; the ledger stays as history.
    spot = load_json(DISC_CAND_PATH)
    shelf = spot.get("shelf") or []
    spot_age = None
    try:
        spot_age = _fmt_age(time.time() - os.path.getmtime(DISC_CAND_PATH))
    except Exception:
        pass
    # world grounding — the toy-vs-world lane (every other gate tests agreement
    # between the system's own runs; this one tests correspondence with the world)
    world = load_json(WORLD_CAL_PATH)
    wrows = db.execute("""
        SELECT UPPER(COALESCE(outcome,status,'?')) o, COALESCE(verified,0) v, COUNT(*) c
        FROM world_groundings GROUP BY 1, 2""").fetchall()
    wout = {}
    for r in wrows:
        k = r["o"] + ("_verified" if r["v"] else "")
        wout[k] = wout.get(k, 0) + r["c"]
    wfails = db.execute("""
        SELECT wg.claim_id, COALESCE(kc.domain,'?') domain,
               substr(COALESCE(kc.claim_summary, kc.hypothesis_text,''),1,140) text
        FROM world_groundings wg JOIN knowledge_claims kc ON kc.id = wg.claim_id
        WHERE UPPER(COALESCE(wg.outcome,wg.status,'')) LIKE '%FAIL%' AND wg.verified=1
        ORDER BY wg.claim_id LIMIT 8""").fetchall()
    # world GATE (2026-07-09): armed state + how many claims it currently caps
    # (a verified world-FAILS blocks ESTABLISHED — the claim is held at REPLICATED)
    wgate = load_json(WORLD_GATE_PATH) or {}
    wgate_capped = db.execute("""
        SELECT COUNT(DISTINCT wg.claim_id) FROM world_groundings wg
        JOIN knowledge_claims kc ON kc.id = wg.claim_id
        WHERE wg.status='resolved' AND wg.outcome='FAILS' AND wg.verified=1
          AND kc.claim_status='REPLICATED'""").fetchone()[0]
    wgate_scope = db.execute("""
        SELECT COUNT(DISTINCT claim_id) FROM world_groundings
        WHERE status='resolved' AND outcome='FAILS' AND verified=1""").fetchone()[0]
    db.close()
    return {"routes": route_rows, "top": top_rows,
            "knocked": {r["route"]: r["c"] for r in knocked},
            "shelf": shelf, "n_shelf": spot.get("n_shelf", len(shelf)), "spot_age": spot_age,
            "world": world, "world_outcomes": wout,
            "world_gate": {"armed": bool(wgate.get("armed")),
                           "capped": wgate_capped, "scope": wgate_scope},
            "world_fails": [dict(r) for r in wfails]}


def get_novelty():
    """Literature audits — verdicts, the burden-flip finder, residues."""
    db = get_db()
    verdicts = db.execute("SELECT verdict, COUNT(*) c FROM novelty_audits GROUP BY verdict").fetchall()
    v = {r["verdict"] or "?": r["c"] for r in verdicts}
    nf_corr = q1(db, "SELECT COUNT(*) FROM novelty_audits WHERE verdict='NOT_FOUND' AND corroborated=1")
    # finder_found is a TEXT citation column ('=1' matched nothing and rendered 0
    # forever while 76 real overrides sat hidden)
    finder_over = q1(db, """
        SELECT COUNT(*) FROM novelty_audits
        WHERE finder_found IS NOT NULL AND TRIM(finder_found) NOT IN ('', '0')""")
    residues = q1(db, "SELECT COUNT(*) FROM novelty_audits WHERE novel_residue IS NOT NULL AND novel_residue != ''")
    res_injected = q1(db, "SELECT COUNT(*) FROM novelty_audits WHERE residue_injected=1")
    recent = db.execute("""
        SELECT na.claim_id, na.verdict, na.confidence, na.corroborated,
               substr(COALESCE(kc.claim_summary, kc.hypothesis_text),1,170) AS text
        FROM novelty_audits na LEFT JOIN knowledge_claims kc ON kc.id = na.claim_id
        WHERE na.verdict='NOT_FOUND' AND na.corroborated=1
        ORDER BY na.created_at DESC LIMIT 6
    """).fetchall()
    db.close()
    cal = load_json(NOVELTY_CAL_PATH)
    return {"verdicts": v, "nf_corroborated": nf_corr, "finder_overrides": finder_over,
            "residues": residues, "residues_injected": res_injected,
            "recent_candidates": [dict(r) for r in recent],
            "ceiling": cal.get("recommended_ceiling", cal.get("novelty_ceiling")),
            "false_novelty_rate": cal.get("false_novelty_rate")}


def get_metaprober():
    """Does the system know what its own claims will do in a new domain?"""
    db = get_db()
    total = q1(db, "SELECT COUNT(*) FROM meta_transfer_predictions")
    outcomes = db.execute("""
        SELECT COALESCE(outcome,'pending') o, COUNT(*) c
        FROM meta_transfer_predictions GROUP BY outcome
    """).fetchall()
    o = {r["o"]: r["c"] for r in outcomes}
    rec = db.execute("""
        SELECT COUNT(*) n,
          SUM(CASE WHEN predicted_direction = observed_direction THEN 1 ELSE 0 END) hit
        FROM meta_transfer_predictions
        WHERE observed_direction IS NOT NULL AND predicted_direction IS NOT NULL
    """).fetchone()
    n, hit = (rec["n"] or 0), (rec["hit"] or 0)
    db.close()
    # canonical reconciliation — what the scorer's transfer haircut actually
    # consumes (a strict predicted==observed recount here disagreed by ~14pp)
    cal = load_json(META_CAL_PATH)
    mech = load_json(MECH_CAL_PATH)
    shapes = []
    for name, m in (mech.get("by_mechanism") or {}).items():
        if isinstance(m, dict) and m.get("n", 0) >= 100:
            shapes.append({"name": name, "n": m["n"],
                           "pct": m.get("confirmation_pct"),
                           "gap": m.get("vs_overall_pp")})
    shapes.sort(key=lambda s: (s["gap"] if s["gap"] is not None else 0))
    return {"total": total, "outcomes": o,
            "reconciled": n, "hits": hit, "accuracy_pct": _pct(hit, n),
            "cal": {"hit_rate": cal.get("hit_rate"),
                    "overconfidence_rate": cal.get("overconfidence_rate"),
                    "n": cal.get("n_scored", cal.get("n"))},
            "mech_overall": mech.get("overall_confirmation_pct"),
            "mech_shapes": shapes[:7]}


def get_scopes():
    """Mapped regimes + the open boundary pool."""
    db = get_db()
    total = q1(db, "SELECT COUNT(*) FROM claim_scopes")
    claims = q1(db, "SELECT COUNT(DISTINCT claim_id) FROM claim_scopes")
    boundary_pool = q1(db, "SELECT COUNT(*) FROM curiosities WHERE provenance='narrowed_boundary' AND status='active'")
    recent = db.execute("""
        SELECT cs.claim_id, substr(cs.scope_text,1,170) AS scope, cs.created_at
        FROM claim_scopes cs ORDER BY cs.created_at DESC LIMIT 6
    """).fetchall()
    db.close()
    return {"total": total, "claims": claims, "boundary_pool": boundary_pool,
            "recent": [dict(r) for r in recent]}


def get_claims_detail():
    db = get_db()
    statuses = db.execute("SELECT status, COUNT(*) c FROM knowledge_claims GROUP BY status ORDER BY c DESC").fetchall()
    post = db.execute("""
        SELECT
          SUM(CASE WHEN posterior < 0.2 THEN 1 ELSE 0 END) b1,
          SUM(CASE WHEN posterior >= 0.2 AND posterior < 0.4 THEN 1 ELSE 0 END) b2,
          SUM(CASE WHEN posterior >= 0.4 AND posterior < 0.6 THEN 1 ELSE 0 END) b3,
          SUM(CASE WHEN posterior >= 0.6 AND posterior < 0.8 THEN 1 ELSE 0 END) b4,
          SUM(CASE WHEN posterior >= 0.8 THEN 1 ELSE 0 END) b5
        FROM knowledge_claims
    """).fetchone()
    cal = db.execute("""
        SELECT champion_auc, champion_brier, trained_at, n_train
        FROM calibration_model_history ORDER BY trained_at DESC LIMIT 1
    """).fetchone()
    db.close()
    return {
        "statuses": {r["status"] or "?": r["c"] for r in statuses},
        "posterior_buckets": [
            {"label": "0–.2", "value": post["b1"] or 0},
            {"label": ".2–.4", "value": post["b2"] or 0},
            {"label": ".4–.6", "value": post["b3"] or 0},
            {"label": ".6–.8", "value": post["b4"] or 0},
            {"label": ".8–1", "value": post["b5"] or 0},
        ],
        "calibration": {
            "auc": cal["champion_auc"] if cal else None,
            "brier": cal["champion_brier"] if cal else None,
            "n_train": cal["n_train"] if cal else 0,
            "age": _fmt_age(time.time() - (cal["trained_at"] or 0)) if cal else "—",
        },
    }


def get_domains():
    db = get_db()
    domains = db.execute("""
        SELECT d.name, d.confidence, d.id,
          (SELECT COUNT(*) FROM experiments e WHERE e.domain = d.name) AS exp_count,
          (SELECT COUNT(*) FROM subtopics s WHERE s.domain_id = d.id) AS topic_count,
          (SELECT COUNT(*) FROM gaps g WHERE g.domain_id = d.id AND g.status='open') AS gap_count
        FROM domains d ORDER BY exp_count DESC, d.confidence DESC
    """).fetchall()
    tiers = {"mastery": [], "emerging": [], "long_tail": []}
    scatter = []
    for d in domains:
        ec = d["exp_count"] or 0
        entry = {"name": (d["name"] or "").replace("_", " "), "confidence": d["confidence"],
                 "exp_count": ec, "topics": d["topic_count"] or 0, "gaps": d["gap_count"] or 0}
        # log-scale tiers — at ~129k experiments the old 5/2/1 cutoffs put every
        # domain in "mastery" and left two panels permanently empty
        if ec >= 1000:
            tiers["mastery"].append(entry)
        elif ec >= 100:
            tiers["emerging"].append(entry)
        else:
            tiers["long_tail"].append(entry)
        if ec > 0:
            scatter.append({"x": ec, "y": round((d["confidence"] or 0.5) * 100),
                            "r": min(max(int(math.sqrt(d["topic_count"] or 0) * 2), 3), 22),
                            "label": entry["name"][:24]})
    db.close()
    scatter.sort(key=lambda b: b["x"], reverse=True)
    return {"tiers": tiers, "scatter": scatter[:80]}


def get_lineage():
    db = get_db()
    depths = db.execute("""
        SELECT evidence_depth, COUNT(*) cnt FROM curiosities
        WHERE status='active' GROUP BY evidence_depth ORDER BY evidence_depth
    """).fetchall()
    depth_dist = [{"depth": d["evidence_depth"] or 0, "count": d["cnt"]} for d in depths]
    chain = []
    # seed the chain from the deepest ACTIVE curiosity so the panel header
    # (max active depth) and the chain shown are the same lineage
    deep = db.execute("""
        SELECT id, text, parent_curiosity_id, evidence_depth FROM curiosities
        WHERE status='active' ORDER BY evidence_depth DESC, created_at DESC LIMIT 1
    """).fetchone()
    if deep:
        chain.append(dict(deep))
        cur = deep
        for _ in range(30):
            if not cur["parent_curiosity_id"]:
                break
            parent = db.execute(
                "SELECT id, text, parent_curiosity_id, evidence_depth FROM curiosities WHERE id = ?",
                (cur["parent_curiosity_id"],)).fetchone()
            if not parent:
                break
            chain.append(dict(parent))
            cur = parent
        chain.reverse()
    transfers = db.execute("""
        SELECT source_domain, target_domain, COUNT(*) cnt FROM transfer_tracking
        WHERE source_domain != target_domain AND source_domain != '__pending__'
          AND COALESCE(target_domain,'') NOT IN ('', '__pending__')
          AND status LIKE 'completed%'
        GROUP BY source_domain, target_domain ORDER BY cnt DESC LIMIT 14
    """).fetchall()
    db.close()
    # bucket depths by 10 to keep the chart readable
    buckets = {}
    for d in depth_dist:
        lo = (d["depth"] // 10) * 10
        b = buckets.setdefault(lo, {"count": 0, "max_depth": d["depth"]})
        b["count"] += d["count"]
        b["max_depth"] = max(b["max_depth"], d["depth"])
    depth_buckets = [{"label": f"{lo}–{lo+9}", "value": b["count"]}
                     for lo, b in sorted(buckets.items())]
    return {"depth_buckets": depth_buckets, "chain": chain,
            "max_depth": max((d["depth"] for d in depth_dist), default=0),
            "transfers": [dict(t) for t in transfers]}


def get_cron():
    try:
        with open(CRON_JOBS_PATH) as f:
            data = json.load(f)
    except Exception:
        return {"jobs": [], "ok": 0, "err": 0, "disabled": 0}
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    now = time.time()
    out, n_ok, n_err, n_dis = [], 0, 0, 0
    for j in jobs:
        last_run = j.get("last_run_at")
        age = "—"
        if last_run:
            try:
                dt = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
                age = _fmt_age(now - dt.timestamp())
            except (ValueError, AttributeError):
                pass
        enabled = j.get("enabled", True)
        status = j.get("last_status", "?")
        if not enabled:
            n_dis += 1
        elif status == "error":
            n_err += 1
        else:
            n_ok += 1
        out.append({"name": j.get("name", "?"), "schedule": j.get("schedule_display", "?"),
                    "enabled": enabled, "status": status,
                    "error": (j.get("last_error") or "")[:180],
                    "no_agent": j.get("no_agent", False), "age": age})
    out.sort(key=lambda x: (x["enabled"], x["status"] != "error", x["name"]))
    return {"jobs": out, "ok": n_ok, "err": n_err, "disabled": n_dis}


def get_rag():
    hourly = query_rag("""
        SELECT strftime('%H:00', queried_at, 'unixepoch', 'localtime') AS hour,
            ROUND(AVG(avg_score), 3) AS avg_score, COUNT(*) AS queries
        FROM query_log WHERE queried_at >= strftime('%s','now') - 86400
        GROUP BY strftime('%Y-%m-%d %H', queried_at, 'unixepoch', 'localtime') ORDER BY MIN(queried_at)
    """)
    total = query_rag("SELECT COUNT(*) c FROM query_log")
    overall = query_rag("SELECT ROUND(AVG(avg_score),3) s FROM query_log")
    high = query_rag("SELECT COUNT(*) c FROM query_log WHERE avg_score > 0.7")
    total_q = total[0]["c"] if total else 0
    server_ok = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:9150/health", timeout=2) as resp:
            server_ok = resp.status == 200
    except Exception:
        pass
    wm = {}
    try:
        w = daemon_query("/status", {})
        if w:
            up = w.get("uptime_seconds", 0)
            wm = {"concepts": w.get("total_concepts", "?"),
                  "associations": w.get("total_associations", "?"),
                  "focus": str(w.get("focus", "?"))[:60],
                  "uptime": f"{int(up//3600)}h {int((up%3600)//60)}m" if isinstance(up, (int, float)) else "?"}
    except Exception:
        wm = {}
    idx_fresh = query_rag("SELECT MAX(indexed_at) t, COUNT(*) n FROM experiments")
    idx_age, idx_n = None, 0
    if idx_fresh and idx_fresh[0].get("t"):
        idx_age = _fmt_age(time.time() - idx_fresh[0]["t"])
        idx_n = idx_fresh[0].get("n") or 0
    return {"hourly": hourly, "total_queries": total_q,
            "overall_avg": (overall[0]["s"] if overall else 0) or 0,
            "high_pct": _pct(high[0]["c"] if high else 0, max(total_q, 1)),
            "server_ok": server_ok, "wm": wm,
            "idx_age": idx_age, "idx_n": idx_n}


def get_system():
    db = get_db()
    now = time.time()
    alerts = []
    cron = get_cron()
    for j in cron["jobs"]:
        if j["status"] == "error":
            alerts.append({"level": "error", "source": "cron", "title": j["name"],
                           "detail": j["error"], "age": j["age"]})
        elif not j["enabled"]:
            alerts.append({"level": "warn", "source": "cron", "title": j["name"],
                           "detail": "disabled", "age": j["age"]})
    res = {}
    try:
        res["db_mb"] = round(os.path.getsize(DB_PATH) / 1048576, 1)
    except Exception:
        res["db_mb"] = 0
    try:
        st = os.statvfs("/")
        tot = st.f_blocks * st.f_bsize
        used = (st.f_blocks - st.f_bavail) * st.f_bsize
        res["disk_pct"] = round(used / tot * 100) if tot else 0
        res["disk_used_gb"] = round(used / 1073741824, 1)
        res["disk_total_gb"] = round(tot / 1073741824, 1)
    except Exception:
        res.update({"disk_pct": 0, "disk_used_gb": 0, "disk_total_gb": 0})
    try:
        mi = {}
        with open("/proc/meminfo") as f:
            for line in f:
                p = line.split(":")
                if len(p) == 2:
                    mi[p[0].strip()] = int(p[1].strip().split()[0]) * 1024
        tot, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        res["mem_pct"] = round((tot - avail) / tot * 100) if tot else 0
        res["mem_used_gb"] = round((tot - avail) / 1073741824, 1)
        res["mem_total_gb"] = round(tot / 1073741824, 1)
        stot, sfree = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
        res["swap_pct"] = round((stot - sfree) / stot * 100) if stot else 0
    except Exception:
        res.update({"mem_pct": 0, "mem_used_gb": 0, "mem_total_gb": 0, "swap_pct": 0})
    # db-lock tripwire — count 'database is locked' in errors.log (the old
    # audit_log query used columns that don't exist and silently rendered 0 forever)
    db_lock_24h = 0
    try:
        errpath0 = os.path.join(LOGS_DIR, "errors.log")
        if os.path.exists(errpath0):
            with open(errpath0) as f:
                tail = f.readlines()[-4000:]
            for line in tail:
                if "database is locked" not in line:
                    continue
                ts = None
                m = re.match(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
                if m:
                    try:
                        ts = datetime.fromisoformat(m.group(1).replace(" ", "T")).timestamp()
                    except ValueError:
                        ts = None
                if ts is None or now - ts <= 86400:
                    db_lock_24h += 1
    except Exception:
        pass
    # worker fleet — running tasks by model (the local-A1 lane vs the mimo default)
    fleet = query_kanban("""
        SELECT COALESCE(model_override,'(fleet default)') model, COUNT(*) n
        FROM tasks WHERE status='running' GROUP BY 1 ORDER BY n DESC""")
    a1_router_on = not os.path.exists(A1_OFF_PATH)
    a1_up = False
    try:
        with urllib.request.urlopen(f"{A1_URL}/health", timeout=2) as resp:
            a1_up = resp.status == 200
    except Exception:
        pass
    # protocol-violation churn — is the goal-loop + bounded-reaper + janitor
    # abandon-guard stack holding? (per-3h buckets, last 24h)
    churn = query_kanban("""
        SELECT CAST(created_at/10800 AS INTEGER)*10800 bucket,
               SUM(kind='protocol_violation') pv, SUM(kind='gave_up') gu,
               SUM(kind='crashed') cr
        FROM task_events
        WHERE kind IN ('protocol_violation','gave_up','crashed')
          AND created_at > strftime('%s','now') - 86400
        GROUP BY bucket ORDER BY bucket""")
    churn_series = [{"label": datetime.fromtimestamp(c["bucket"]).strftime("%H"),
                     "value": (c["pv"] or 0) + (c["gu"] or 0) + (c["cr"] or 0)} for c in churn]
    churn_now = churn_series[-1]["value"] if churn_series else 0
    # backups — rolling-10 freshness for both DBs
    backups = {}
    for label, sub, cadence_min in (("prometheus", "prometheus-db", 15), ("kanban", "kanban-db", 30)):
        d0 = os.path.join(BACKUPS_DIR, sub)
        try:
            files = [os.path.join(d0, x) for x in os.listdir(d0) if x.endswith(".db")]
            newest = max((os.path.getmtime(p) for p in files), default=0)
            backups[label] = {"n": len(files), "age": _fmt_age(now - newest) if newest else "—",
                              "stale": bool(newest and now - newest > cadence_min * 60 * 2)}
        except Exception:
            backups[label] = {"n": 0, "age": "—", "stale": True}
    errors = []
    errpath = os.path.join(LOGS_DIR, "errors.log")
    if os.path.exists(errpath):
        try:
            with open(errpath) as f:
                lines = f.readlines()
            for line in lines[-12:]:
                line = line.strip()
                if not line:
                    continue
                lvl = "warn" if "WARNING" in line else (
                    "error" if any(k in line for k in ("ERROR", "Traceback", "Exception", "CRITICAL")) else None)
                if lvl:
                    errors.append({"level": lvl, "text": line[:220]})
        except Exception:
            pass
    db.close()
    return {"alerts": alerts, "resources": res, "errors": errors,
            "db_lock_24h": db_lock_24h,
            "fleet": fleet, "a1_router_on": a1_router_on, "a1_up": a1_up,
            "churn_series": churn_series, "churn_now": churn_now,
            "backups": backups,
            "cron_summary": {"ok": cron["ok"], "err": cron["err"], "disabled": cron["disabled"]}}


# ═════════════════════════════════════════════════════════════════
# SSR SVG charts — precomputed in Python, zero JS
# ═════════════════════════════════════════════════════════════════

def svg_bars(items, width=680, height=150, color=SIGNAL, fmt=_fmt_num):
    """Vertical bars. items = [{label, value}]"""
    if not items:
        return '<div class="empty">no data</div>'
    n = len(items)
    maxv = max((it["value"] for it in items), default=1) or 1
    pad_b, pad_t = 22, 14
    bw = width / n
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for i, it in enumerate(items):
        h = (height - pad_b - pad_t) * it["value"] / maxv
        x = i * bw + bw * 0.14
        y = height - pad_b - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw*0.72:.1f}" height="{max(h,1.5):.1f}" rx="2"'
            f' fill="{color}"><title>{_esc(it["label"])}: {fmt(it["value"])}</title></rect>')
        if it["value"] and n <= 20:
            parts.append(f'<text x="{i*bw+bw/2:.1f}" y="{y-4:.1f}" class="cnum" text-anchor="middle">{fmt(it["value"])}</text>')
        step = max(1, n // 12)
        if i % step == 0:
            parts.append(f'<text x="{i*bw+bw/2:.1f}" y="{height-6}" class="clab" text-anchor="middle">{_esc(it["label"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_split_bars(items, width=680, height=150):
    """Hourly bars with a coral refuted layer under a teal total."""
    if not items:
        return '<div class="empty">no data</div>'
    n = len(items)
    maxv = max((it["total"] for it in items), default=1) or 1
    pad_b, pad_t = 22, 8
    bw = width / n
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for i, it in enumerate(items):
        th = (height - pad_b - pad_t) * it["total"] / maxv
        rh = (height - pad_b - pad_t) * it["refuted"] / maxv
        x = i * bw + bw * 0.16
        w = bw * 0.68
        parts.append(
            f'<rect x="{x:.1f}" y="{height-pad_b-th:.1f}" width="{w:.1f}" height="{max(th,1):.1f}" rx="2" fill="{SIGNAL}">'
            f'<title>{_esc(it["label"])}:00 — {it["total"]} experiments, {it["refuted"]} refuted</title></rect>')
        if rh > 0.5:
            parts.append(f'<rect x="{x:.1f}" y="{height-pad_b-rh:.1f}" width="{w:.1f}" height="{rh:.1f}" rx="2" fill="{BAD}" opacity="0.9"/>')
        if i % 3 == 0:
            parts.append(f'<text x="{i*bw+bw/2:.1f}" y="{height-6}" class="clab" text-anchor="middle">{_esc(it["label"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_hbar(pairs, width=680, row_h=26, fmt=_fmt_num, palette=None):
    """Horizontal labeled bars. pairs = [(label, value, color?)]"""
    if not pairs:
        return '<div class="empty">no data</div>'
    maxv = max((p[1] for p in pairs), default=1) or 1
    height = row_h * len(pairs) + 4
    label_w = 190
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for i, p in enumerate(pairs):
        label, value = p[0], p[1]
        color = p[2] if len(p) > 2 else (palette[i % len(palette)] if palette else SIGNAL)
        y = i * row_h + 4
        bw = (width - label_w - 74) * value / maxv
        parts.append(f'<text x="{label_w-8}" y="{y+row_h*0.62:.1f}" class="clab" text-anchor="end">{_esc(str(label)[:30])}</text>')
        parts.append(f'<rect x="{label_w}" y="{y+4}" width="{max(bw,1.5):.1f}" height="{row_h-11}" rx="2" fill="{color}">'
                     f'<title>{_esc(label)}: {fmt(value)}</title></rect>')
        parts.append(f'<text x="{label_w+max(bw,1.5)+7:.1f}" y="{y+row_h*0.62:.1f}" class="cnum">{fmt(value)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_donut(pairs, size=150, hole=0.62):
    """Donut. pairs = [(label, value, color)]"""
    total = sum(p[1] for p in pairs)
    if not total:
        return '<div class="empty">no data</div>'
    r = size / 2 - 4
    cx = cy = size / 2
    circ = 2 * math.pi * r
    parts = [f'<svg viewBox="0 0 {size} {size}" class="donut" role="img">']
    offset = circ * 0.25  # start at 12 o'clock
    for label, value, color in pairs:
        frac = value / total
        dash = frac * circ
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}"'
            f' stroke-width="{r*(1-hole):.1f}" stroke-dasharray="{dash:.2f} {circ-dash:.2f}"'
            f' stroke-dashoffset="{offset:.2f}"><title>{_esc(label)}: {_fmt_num(value)} ({frac*100:.0f}%)</title></circle>')
        offset -= dash
    parts.append(f'<text x="{cx}" y="{cy+5}" text-anchor="middle" class="dnum">{_fmt_num(total)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_scatter(points, width=680, height=240):
    """Domain scatter: x = experiments (log), y = confidence."""
    if not points:
        return '<div class="empty">no data</div>'
    maxx = max((p["x"] for p in points), default=1)
    lmax = math.log10(maxx + 1)
    pad_l, pad_b, pad_t, pad_r = 40, 24, 8, 12
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img">']
    for gy in (0, 25, 50, 75, 100):
        y = pad_t + (height - pad_b - pad_t) * (1 - gy / 100)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l-6}" y="{y+3.5:.1f}" class="clab" text-anchor="end">{gy}</text>')
    for p in points:
        x = pad_l + (width - pad_l - pad_r) * (math.log10(p["x"] + 1) / lmax if lmax else 0)
        y = pad_t + (height - pad_b - pad_t) * (1 - p["y"] / 100)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{p["r"]}" fill="{SIGNAL}" fill-opacity="0.55" stroke="{SIGNAL_DK}" stroke-width="1">'
                     f'<title>{_esc(p["label"])} — {p["x"]} experiments, confidence {p["y"]}%</title></circle>')
    parts.append(f'<text x="{(pad_l+width-pad_r)/2}" y="{height-6}" class="clab" text-anchor="middle">experiments (log scale) →</text>')
    parts.append("</svg>")
    return "".join(parts)


def meter(pct, good_low=False, width_class=""):
    """A thin horizontal meter; coral when in the bad zone."""
    p = max(0, min(100, pct or 0))
    bad = (p >= 85) if not good_low else (p <= 15)
    color = BAD if bad else ACCENT
    return (f'<div class="meter {width_class}"><span style="width:{p}%;background:{color}"></span></div>')


# ═════════════════════════════════════════════════════════════════
# render — panels, sections, page
# ═════════════════════════════════════════════════════════════════

_FIG = {"n": 0}


def fig(title, body_html, caption=""):
    _FIG["n"] += 1
    cap = f'<figcaption><span class="fign">Fig. {_FIG["n"]}</span> {caption}</figcaption>' if caption else ""
    return f'<figure class="panel"><h3>{title}</h3>{body_html}{cap}</figure>'


def panel(title, body_html, note=""):
    n = f'<div class="pnote">{note}</div>' if note else ""
    return f'<div class="panel"><h3>{title}</h3>{body_html}{n}</div>'


def stat(label, value, sub=""):
    s = f'<div class="stat-sub">{sub}</div>' if sub else ""
    return f'<div class="stat"><div class="stat-v">{value}</div><div class="stat-l">{label}</div>{s}</div>'


def badge(text, kind=""):
    return f'<span class="badge {kind}">{_esc(text)}</span>'


def err_panel(name, err):
    return (f'<div class="panel"><h3>{_esc(name)}</h3>'
            f'<div class="subblock bad">section unavailable — {_esc(str(err)[:220])}</div></div>')


def render_overview(d):
    s, series, feed, gates = d["stats"], d["series"], d["feed"], d["gates"]
    v = s.get("verdicts", {}) or {}
    vd = [("confirmed", v.get("confirmed") or 0, VERDICT_COLORS["confirmed"]),
          ("supported", v.get("supported") or 0, VERDICT_COLORS["supported"]),
          ("partial", v.get("partial") or 0, VERDICT_COLORS["partial"]),
          ("refuted", v.get("refuted") or 0, VERDICT_COLORS["refuted"])]
    legend = "".join(f'<span class="lg"><i style="background:{c}"></i>{l} {_fmt_num(n)}</span>' for l, n, c in vd)
    total_v = sum(x[1] for x in vd)
    refute_pct = _pct(v.get("refuted") or 0, total_v)
    tr = gates.get("transfer", {}) if isinstance(gates, dict) else {}
    stats_row = (
        stat("experiments", _fmt_num(s["exp_count"]), f'{s["exp_last_hour"]}/hr · {s["exp_last_5min"]} in 5m')
        + stat("claims", _fmt_num(s["kc_count"]),
               f'{_fmt_num(s.get("candidates_sci", s["tiers"].get("CANDIDATE",0)))} sci candidates · '
               f'{_fmt_num(s["tiers"].get("CANDIDATE",0) - s.get("candidates_sci", 0))} meta/lookup')
        + stat("shelf", f'{s["replicated_shelf"]} <span class="dim">R</span> · {s["established_shelf"]} <span class="dim">E</span>',
               "replicated · established (excl. meta/lookup)")
        + stat("transfer survival", f'{tr.get("survival_pct","—")}%',
               f'{_fmt_num(tr.get("replicated",0))} replicated · {_fmt_num(tr.get("total",0))} x-domain done')
        + stat("refute rate", f"{refute_pct}%", "refutation drives mutation")
        + stat("workers", s["active_workers"],
               f'alive now (heartbeat <2m) · {s.get("running_tasks", "?")} running · cap ~20')
    )
    feed_rows = "".join(
        f'<div class="feed-row v-{f["verdict"] or "none"}">'
        f'<span class="ft">{f["time"]}</span><span class="fd">{_esc(f["domain"][:22])}</span>'
        f'<span class="fh">{_esc(f["hyp"])}</span></div>'
        for f in feed[:14])
    topo = s.get("topology")
    topo_note = (f'{_fmt_num(topo["nodes"])} nodes · {_fmt_num(topo["edges"])} edges · '
                 f'{topo["communities"]} communities — see the <a href="/plate/prometheus-topology.html">topology plate</a>'
                 if topo else "topology export not found")
    return (
        f'<div class="stats-row">{stats_row}</div>'
        + fig("The last 24 hours", svg_split_bars(series["hours"]),
              "experiments per hour; the coral layer is refutations. A healthy loop refutes.")
        + f'<div class="two">'
        + fig("Verdicts, all time", svg_donut([(l, n, c) for l, n, c in vd]) + f'<div class="legend">{legend}</div>',
              "completed experiments by primary verdict.")
        + panel("Now on the wire", f'<div class="feed">{feed_rows}</div>', "newest experiments; color bar = verdict.")
        + "</div>"
        + panel("Knowledge topology", topo_note)
    )


def render_gates(g):
    at = g["attacks"]
    arb = g["arbitration"]
    arb_pairs = [("resolved → A", arb.get("resolved_a", 0), SLATE),
                 ("resolved → B", arb.get("resolved_b", 0), SLATE),
                 ("regime split", arb.get("regime_split", 0), OCHRE),
                 ("both wrong", arb.get("both_wrong", 0), BAD),
                 ("pending", arb.get("pending", 0), FAINT),
                 ("expired", arb.get("expired", 0), FAINT)]
    repl = g["replication"]
    repl_pairs = [(k, repl[k], SIGNAL if "confirm" in k.lower() or "replic" in k.lower() else (BAD if "disagre" in k.lower() or "refut" in k.lower() else OCHRE))
                  for k in sorted(repl, key=lambda k: -repl[k])][:7]
    tr = g["transfer"]
    ry = g.get("retest_yield", {})
    ry_pct = ry.get("pct")
    mc = g.get("method_code", {})
    fam_short = {"deepseek/deepseek-v4-flash": "deepseek", "agents-a1": "agents-a1 (local)",
                 "(legacy/mimo)": "(legacy/mimo)"}
    atk_pairs = [(f'{fam_short.get(a["model"], a["model"].split("/")[-1].split(":")[0])} '
                  f'({a["sv"] or 0}s/{a["rf"] or 0}b)', a["n"],
                  SIGNAL if a["model"] == "agents-a1" else (SLATE if "deepseek" in a["model"] else FAINT))
                 for a in g.get("attackers", [])]
    atk48 = " · ".join(f'{fam_short.get(a["model"], a["model"].split("/")[-1].split(":")[0])} {a["n"]}'
                       for a in g.get("attackers_48h", [])[:5]) or "none"
    mc_rows = "".join(
        f'<div class="disc"><div class="disc-head"><b>#{r["id"]}</b> {badge(r["claim_status"] or "?")}</div>'
        f'<div class="disc-text">{_esc(r["reason"] or "")}</div></div>'
        for r in g.get("method_code_flagged_rows", []))
    sconv = g.get("scoped_conv", {})
    return (
        '<div class="stats-row">'
        + stat("attack survival", f'{g["attack_survival_pct"]}%',
               f'{at["survived"]} survived · {at["refuted"]} broke · {at["narrowed"]} narrowed')
        + stat("retest credit yield", f'{ry_pct}%' if ry_pct is not None else "—",
               f'{ry.get("credited",0)}/{ry.get("done",0)} completed retests credited 24h · <85% = leak')
        + stat("retested claims", _fmt_num(g["retested_claims"]), "≥1 independent retest")
        + stat("disputed now", _fmt_num(g["disputed_now"]), f'{g["arb_24h"]} arbitrations filed 24h')
        + stat("transfer survival", f'{tr["survival_pct"]}%',
               f'{_fmt_num(tr["replicated"])} replicated · {_fmt_num(tr["disagreed"])} disagreed · {_fmt_num(tr["total"])} x-domain done')
        + stat("method ≠ code", _fmt_num(mc.get("flagged", 0)),
               f'{_fmt_num(mc.get("reviews",0))} reviews · capped at CANDIDATE (I4 gate)')
        + stat("circular construction", _fmt_num(g.get("circular_flagged", 0)), "self-fulfilling designs, capped")
        + stat("contradiction lane", g["contradiction"]["enqueued"], f'of {g["contradiction"]["total"]} filed — literature says no')
        + "</div>"
        + '<div class="two">'
        + fig("Adversarial attacks", svg_hbar([
            ("survived", at["survived"], SIGNAL),
            ("narrowed", at["narrowed"], OCHRE),
            ("broke", at["refuted"], BAD),
            ("broke → arbitrated", at.get("refuted_arbitrated", 0), MAUVE),
            ("expired", at.get("expired", 0), FAINT),
            ("in flight", at["pending"], STEEL)]),
            f'cross-family attackers (independence-gated pick) try to break promoted claims; '
            f'{_fmt_num(g.get("attack_saturated",0))} claims attack-saturated (≥5 narrows, terminal cap).')
        + fig("Dispute arbitration", svg_hbar(arb_pairs),
              "decisive experiments on DISPUTED claims. Regime splits map where both sides were right.")
        + "</div>"
        + '<div class="two">'
        + fig("Who attacks — the cross-family pool", svg_hbar(atk_pairs),
              f'per attacker: total (survived/broke). Last 48h mix: {atk48}. '
              f'No family grades its own lineage\'s work.')
        + fig("Retest gate", svg_hbar(repl_pairs),
              "replication results — independent re-runs before promotion.")
        + "</div>"
        + panel("Method-code alignment (the 5th gate)",
                mc_rows or '<div class="empty">no mismatches — every reviewed claim\'s code implements its described method</div>',
                f'{_fmt_num(mc.get("aligned",0))} aligned · {_fmt_num(mc.get("flagged",0))} flagged: majority of cross-family judges '
                f'found the preserved code does not implement the method the finding describes. Flagged claims cap at CANDIDATE.')
        + panel("False consensus (spurious agreement)",
                f'<div class="kv">mean SA <b>{g["sa"].get("avg_sa","—")}</b> over {_fmt_num(g["sa"].get("has_sa",0))} scored claims · '
                f'<b class="{"warn" if g["sa_high_promotion"] else ""}">{_fmt_num(g["sa_high_promotion"])}</b> high-SA claims in the promotion band (settlement-lane intake) · '
                f'scoped-claim convergence: <b>{sconv.get("converged",0)}/{sconv.get("total",0)}</b> converged · '
                f'{sconv.get("mapping",0)} mapping · <b class="{"warn" if sconv.get("orbiting",0) > 0.1*max(sconv.get("total",1),1) else ""}">{sconv.get("orbiting",0)}</b> orbiting</div>')
    )


def render_independence(ind):
    a = ind["armed"] or {}
    armed = bool(a.get("armed"))
    th = a.get("thresholds", {})
    stamps = a.get("stamps", ind["stamps_total"])
    min_stamps = th.get("min_stamps", 500)
    prog = min(100, _pct(stamps, min_stamps))
    armed_html = (
        f'<div class="armed {"on" if armed else ""}">'
        f'<div class="armed-state">{"ARMED" if armed else "NOT ARMED"}</div>'
        f'<div class="armed-detail">accuracy {a.get("accuracy","—")} (needs ≥ {th.get("accuracy",0.98)}) · '
        f'{_fmt_num(a.get("audited",0))} audited (needs ≥ {th.get("min_audit",50)}) · '
        f'{_fmt_num(stamps)}/{_fmt_num(min_stamps)} stamps</div>'
        f'{meter(prog)}'
        f'<div class="pnote">arms itself the instant thresholds cross — a check, not a timer. '
        f'checked {_fmt_age(time.time() - a.get("checked_at", 0)) if a.get("checked_at") else "—"} ago</div></div>')
    fed_pct = _pct(ind["stamps_fed"], ind["stamps_total"])
    return (
        '<div class="stats-row">'
        + stat("stamps", _fmt_num(ind["stamps_total"]), f'{ind["stamps_24h"]} in 24h · every new task, no gate')
        + stat("prior-fed", f"{fed_pct}%", "task bodies carrying the RAG feed")
        + stat("claims priced", _fmt_num(ind["ci_n"]), f'{ind["ci_bitten"]} with multiplier < 1.0')
        + stat("mean multiplier", ind["ci_avg"] if ind["ci_avg"] is not None else "—", "1.0 = fully blind support")
        + stat("monoculture", f'{ind["mono_pct"]}%', f'{_fmt_num(ind["mono"])} single-family claims')
        + "</div>"
        + panel("The self-arming haircut", armed_html,
                "when armed, discovery-shelf support depth is multiplied by per-claim independence (0.5–1.0). Neutral until then.")
        + fig("Independence multiplier distribution",
              svg_hbar([(b["label"], b["value"],
                         SIGNAL if "blind" in b["label"] else (BAD if "all-fed" in b["label"] else OCHRE))
                        for b in ind["mult_buckets"]]),
              "how much of each claim's support arrived with the prior findings already in the prompt.")
        + panel("Honest limits",
                '<div class="pnote">the stamp only recovers contamination going forward; model-family independence is weak '
                '(shared training data); the deepest common cause — the shared base model — no internal agreement removes.</div>')
    )


def render_discovery(disc):
    route_names = {"discovery": ("discovery", SIGNAL), "(legacy)": ("(legacy)", FAINT),
                   "derivable": ("derivable — off shelf", MAUVE),
                   "known_in_lit": ("known in literature — off shelf", SLATE),
                   "search_miss": ("likely search miss", OCHRE)}
    agg = {}
    for r in disc["routes"]:
        agg[r["route"]] = agg.get(r["route"], 0) + r["c"]
    pairs = [(route_names.get(k, (k, FAINT))[0], n, route_names.get(k, (k, FAINT))[1])
             for k, n in sorted(agg.items(), key=lambda kv: -kv[1])]
    shelf_rows = "".join(
        f'<div class="disc"><div class="disc-head"><b>#{r.get("claim_id")}</b> '
        f'{badge(f"score {round(_safe_float(r.get("score")),1)}")} {badge(r.get("tier") or "?")} '
        f'{badge((r.get("domain") or "?")[:24])} '
        f'{badge(f"{r.get("survivals") or 0} survivals")}'
        f'{" " + badge("sim-internal", "bad") if str(r.get("sim_flagged")) == "True" else ""}</div>'
        f'<div class="disc-text">{_esc((r.get("summary") or "")[:190])}</div></div>'
        for r in (disc.get("shelf") or [])[:8])
    w = disc.get("world") or {}
    wo = disc.get("world_outcomes") or {}
    rate = w.get("world_agreement_rate")
    fail_rows = "".join(
        f'<div class="disc"><div class="disc-head"><b>#{r["claim_id"]}</b> {badge(r["domain"][:22])} '
        f'{badge("world says NO", "bad")}</div>'
        f'<div class="disc-text">{_esc(r["text"])}</div></div>'
        for r in disc.get("world_fails") or [])
    world_panel = (
        '<div class="stats-row">'
        + stat("world agreement", f'{round(rate*100)}%' if isinstance(rate, (int, float)) else "—",
               "verified HOLDS / (HOLDS+FAILS) on real published data")
        + stat("verified holds", _fmt_num(wo.get("HOLDS_verified", 0)),
               f'{_fmt_num(wo.get("HOLDS", 0))} more unverified')
        + stat("verified fails", _fmt_num(wo.get("FAILS_verified", 0)),
               f'{_fmt_num(wo.get("FAILS", 0))} more unverified — the treasure: confident + wrong')
        + stat("no dataset", _fmt_num(wo.get("NO_DATASET_verified", 0) + wo.get("NO_DATASET", 0)),
               "maps the toy-boundary")
        + stat("world gate",
               ("ARMED" if (disc.get("world_gate") or {}).get("armed") else "off"),
               (lambda wg: f'caps {wg.get("capped", 0)}/{wg.get("scope", 0)} verified-FAILS claims '
                           f'from ESTABLISHED' if wg.get("armed")
                           else "report-only")(disc.get("world_gate") or {}))
        + "</div>"
        + panel("Reality's refusals — verified world-FAILS",
                fail_rows or '<div class="empty">none verified yet</div>',
                "claims that passed every internal gate and still failed on real external data. "
                "No domain/method cluster found at n=6 — the shared thread is toy-vs-world.")
    )
    return (
        panel(f'The shelf — spotlight fresh routing <span class="dim">({disc.get("n_shelf", 0)} claims'
              + (f', rebuilt {disc["spot_age"]} ago' if disc.get("spot_age") else "") + ")</span>",
              shelf_rows or '<div class="empty">no discovery-route claims on the fresh pass</div>',
              'authoritative shelf = the hourly fresh-routing pass (discovery_candidates.json); '
              'full editorial treatment on the <a href="/plate/prometheus-discoveries.html">discoveries plate</a>.')
        + fig("Routing ledger — cumulative, all passes", svg_hbar(pairs),
              "the discovery terminus history: only the discovery route stays on the shelf; the rest are knockouts, "
              "shown, not hidden. Ledger rows predating a router change linger here — the shelf above is the live truth.")
        + panel("World grounding — the toy-vs-world lane", world_panel,
                "every other gate tests agreement between the system's own runs; this lane tests correspondence "
                "with the world. Report-only: binds nothing yet.")
    )


def render_novelty(nov):
    v = nov["verdicts"]
    pairs = [("PARTIALLY_KNOWN", v.get("PARTIALLY_KNOWN", 0), SLATE),
             ("NOT_FOUND", v.get("NOT_FOUND", 0), OCHRE),
             ("KNOWN", v.get("KNOWN", 0), FAINT),
             ("CONTRADICTED", v.get("CONTRADICTED", 0), BAD)]
    cand_rows = "".join(
        f'<div class="disc"><div class="disc-head"><b>#{r["claim_id"]}</b> '
        f'{badge("corroborated", "ok")} {badge(f"conf {round(_safe_float(r["confidence"]),2)}")}</div>'
        f'<div class="disc-text">{_esc(r["text"] or "")}</div></div>'
        for r in nov["recent_candidates"])
    total = sum(v.values()) or 1
    return (
        '<div class="stats-row">'
        + stat("audited", _fmt_num(total), "promoted claims literature-checked")
        + stat("corroborated NOT_FOUND", _fmt_num(nov["nf_corroborated"]), "two families searched; neither found it")
        + stat("finder overrides", _fmt_num(nov["finder_overrides"]), "third family FOUND the paper — novelty refuted")
        + stat("residues", _fmt_num(nov["residues"]), f'{_fmt_num(nov["residues_injected"])} injected as new questions')
        + stat("novelty ceiling", nov.get("ceiling") if nov.get("ceiling") is not None else "—",
               f'false-novelty rate {nov.get("false_novelty_rate")}' if nov.get("false_novelty_rate") is not None
               else "shelf-level trust from index-armed adjudication")
        + "</div>"
        + fig("Literature verdicts", svg_hbar(pairs),
              "single-pass web search, model-judged — absence of evidence, not established novelty.")
        + panel("Corroborated novelty candidates", cand_rows or '<div class="empty">none yet</div>',
                "NOT_FOUND twice, independently. A candidate pool, not a prize.")
    )


def render_metaprober(mp):
    o = mp["outcomes"]
    pairs = [("survived", o.get("survived", 0), SIGNAL),
             ("narrowed", o.get("narrowed", 0), OCHRE),
             ("refuted", o.get("refuted", 0), BAD),
             ("pending", o.get("pending", 0), FAINT)]
    cal = mp.get("cal") or {}
    hr = cal.get("hit_rate")
    oc = cal.get("overconfidence_rate")
    shape_pairs = [(f'{s["name"]} ({_fmt_num(s["n"])})', abs(s["gap"] or 0),
                    BAD if (s["gap"] or 0) < -2 else (SIGNAL if (s["gap"] or 0) > 2 else FAINT))
                   for s in mp.get("mech_shapes") or []]
    return (
        '<div class="stats-row">'
        + stat("predictions", _fmt_num(mp["total"]), "meta-claims probed against fresh domains")
        + stat("reconciled", _fmt_num(mp["reconciled"]), "observed direction recorded")
        + stat("hit rate (canonical)", f'{round(hr*100,1)}%' if isinstance(hr, (int, float)) else f'{mp["accuracy_pct"]}%',
               "meta_transfer_calibration.json — what the scorer's haircut consumes")
        + stat("overconfident", f'{round(oc*100,1)}%' if isinstance(oc, (int, float)) else "—",
               "predicted transfer, world said no — drives the p_confirm haircut")
        + stat("strict recount", f'{mp["accuracy_pct"]}%',
               f'{mp["hits"]}/{mp["reconciled"]} exact direction matches (this panel\'s own math)')
        + "</div>"
        + fig("What actually happened to its predictions", svg_hbar(pairs),
              "the system predicts how its own meta-claims will transfer, then checks. Near-coin-flip accuracy is the honest finding.")
        + fig("Prior trust by mechanism shape — |gap| vs overall confirmation", svg_hbar(shape_pairs),
              f'overall {mp.get("mech_overall","—")}% confirmation; coral bars = over-trusted shapes '
              f'(confirm less than average — MONOTONIC is the worst offender), green = under-trusted. '
              f'Feeds the scorer\'s mechanism-shape haircut.')
    )


def render_scopes(sc):
    rows = "".join(
        f'<div class="disc"><div class="disc-head"><b>#{r["claim_id"]}</b> '
        f'<span class="dim">{_fmt_age(time.time() - _safe_float(r["created_at"]))} ago</span></div>'
        f'<div class="disc-text">{_esc(r["scope"])}</div></div>'
        for r in sc["recent"])
    return (
        '<div class="stats-row">'
        + stat("scopes mapped", _fmt_num(sc["total"]), f'across {_fmt_num(sc["claims"])} claims')
        + stat("boundary pool", _fmt_num(sc["boundary_pool"]), "open questions from narrowed attacks")
        + "</div>"
        + panel("Recently mapped regimes", rows or '<div class="empty">none yet</div>',
                "every NARROWED verdict writes the regime where the claim still holds — the map grows at the edges.")
    )


def render_claims(c):
    st = c["statuses"]
    pairs = [(k, st[k], MINERALS[i % len(MINERALS)]) for i, k in enumerate(sorted(st, key=lambda k: -st[k])[:8])]
    cal = c["calibration"]
    return (
        '<div class="two">'
        + fig("Claim lifecycle", svg_hbar(pairs), "every hypothesis the fleet has ever weighed, by status.")
        + fig("Posterior distribution", svg_bars(c["posterior_buckets"], height=170),
              "belief mass across the shelf; healthy = most claims uncommitted.")
        + "</div>"
        + panel("Calibration model",
                f'<div class="kv">AUC <b>{cal["auc"] if cal["auc"] is not None else "—"}</b> · '
                f'Brier <b>{cal["brier"] if cal["brier"] is not None else "—"}</b> · '
                f'trained on {_fmt_num(cal["n_train"])} · {cal["age"]} ago</div>')
    )


def render_domains(dm):
    t = dm["tiers"]

    def tier_rows(entries, limit=24):
        return "".join(
            f'<div class="dom-row"><span class="dom-name">{_esc(e["name"][:34])}</span>'
            f'<span class="dom-n">{e["exp_count"]}</span>'
            f'<span class="dom-meta">{e["topics"]} topics · {e["gaps"]} gaps</span></div>'
            for e in entries[:limit])
    return (
        fig("Where the experiments land", svg_scatter(dm["scatter"]),
            "each dot a domain; size = subtopics, height = confidence.")
        + '<div class="three">'
        + panel(f'Heavy <span class="dim">({len(t["mastery"])})</span>', tier_rows(t["mastery"]), "≥1,000 experiments")
        + panel(f'Active <span class="dim">({len(t["emerging"])})</span>', tier_rows(t["emerging"]), "100–999 experiments")
        + panel(f'Long tail <span class="dim">({len(t["long_tail"])})</span>', tier_rows(t["long_tail"], 18), "<100 experiments")
        + "</div>"
    )


def render_lineage(lin):
    chain_html = ""
    if lin["chain"]:
        steps = "".join(
            f'<div class="chain-step"><span class="chain-d">d{c["evidence_depth"] or 0}</span>'
            f'<span>{_esc((c["text"] or "")[:150])}</span></div>'
            for c in lin["chain"][-12:])
        chain_html = panel(f'Deepest live lineage <span class="dim">(depth {lin["max_depth"]})</span>',
                           f'<div class="chain">{steps}</div>',
                           "each question born from the last one's evidence.")
    transfers = "".join(
        f'<div class="dom-row"><span class="dom-name">{_esc((t["source_domain"] or "?")[:22])} → {_esc((t["target_domain"] or "?")[:22])}</span>'
        f'<span class="dom-n">{t["cnt"]}</span></div>'
        for t in lin["transfers"])
    return (
        fig("Active curiosity depth", svg_bars(lin["depth_buckets"], height=170),
            "how many questions deep the live frontier runs, bucketed by 10.")
        + '<div class="two">'
        + chain_html
        + panel("Busiest transfer routes", transfers or '<div class="empty">none</div>', "cross-domain claim traffic.")
        + "</div>"
    )


def render_activity(feed):
    rows = "".join(
        f'<div class="feed-row v-{f["verdict"] or "none"}">'
        f'<span class="ft">{f["time"]}</span><span class="fd">{_esc(f["domain"][:24])}</span>'
        f'<span class="fh">{_esc(f["hyp"])}</span>'
        f'<span class="fc {"neg" if f["cc"] < 0 else ""}">{f["cc"]:+.2f}</span></div>'
        for f in feed)
    return panel("Experiment feed", f'<div class="feed tall">{rows}</div>',
                 "newest first; color bar = verdict, right column = confidence change.")


def render_cron(cr):
    rows = "".join(
        f'<tr class="{"err" if j["status"]=="error" else ("dis" if not j["enabled"] else "")}">'
        f'<td>{_esc(j["name"])}</td><td class="mono">{_esc(j["schedule"])}</td>'
        f'<td>{badge("script" if j["no_agent"] else "agent")}</td>'
        f'<td>{badge(j["status"], "bad" if j["status"]=="error" else ("ok" if j["status"]=="ok" else ""))}'
        f'{" " + badge("disabled", "bad") if not j["enabled"] else ""}</td>'
        f'<td class="mono">{j["age"]}</td>'
        f'<td class="errcell">{_esc(j["error"][:90])}</td></tr>'
        for j in cr["jobs"])
    return (
        '<div class="stats-row">'
        + stat("healthy", cr["ok"]) + stat("erroring", cr["err"]) + stat("disabled", cr["disabled"])
        + "</div>"
        + panel("The metabolism",
                f'<div class="scrollx"><table><thead><tr><th>job</th><th>schedule</th><th>kind</th>'
                f'<th>status</th><th>last run</th><th>error</th></tr></thead><tbody>{rows}</tbody></table></div>',
                "every job in ~/.hermes/cron/jobs.json — errors and disabled jobs sort first.")
    )


def render_rag(r):
    hourly = [{"label": h["hour"][:2] if h.get("hour") else "?", "value": h.get("queries") or 0} for h in r["hourly"]]
    wm = r.get("wm") or {}
    wm_html = (f'<div class="kv">concepts <b>{wm.get("concepts","?")}</b> · associations <b>{wm.get("associations","?")}</b> · '
               f'up <b>{wm.get("uptime","?")}</b><br><span class="dim">focus: {_esc(wm.get("focus","?"))}</span></div>'
               if wm else '<div class="empty">working-memory daemon unreachable</div>')
    return (
        '<div class="stats-row">'
        + stat("queries", _fmt_num(r["total_queries"]), "all time")
        + stat("mean score", r["overall_avg"], f'{r["high_pct"]}% above 0.7')
        + stat("embed server", "up" if r["server_ok"] else "DOWN", ":9150 " + ("healthy" if r["server_ok"] else "unreachable"))
        + stat("index freshness", r.get("idx_age") or "—",
               f'{_fmt_num(r.get("idx_n") or 0)} experiments indexed; newest this long ago')
        + "</div>"
        + fig("RAG queries, last 24h", svg_bars(hourly, height=150),
              "the knowledge feed under the fleet — every task body starts here.")
        + panel("Working memory", wm_html)
    )


def render_system(sy):
    res = sy["resources"]
    alerts = "".join(
        f'<div class="subblock {"bad" if a["level"]=="error" else ""}">'
        f'<b>{_esc(a["title"])}</b> <span class="dim">({a["source"]}, {a["age"]})</span><br>{_esc(a["detail"][:160])}</div>'
        for a in sy["alerts"][:10]) or '<div class="empty">no alerts</div>'
    errors = "".join(
        f'<div class="subblock {"bad" if e["level"]=="error" else ""} mono small">{_esc(e["text"])}</div>'
        for e in sy["errors"][-8:]) or '<div class="empty">error log quiet</div>'
    fleet = sy.get("fleet") or []
    fleet_pairs = [((f["model"] or "?").split("/")[-1].split(":")[0], f["n"],
                    SIGNAL if f["model"] == "agents-a1" else SLATE) for f in fleet]
    n_running = sum(f["n"] for f in fleet)
    n_a1 = sum(f["n"] for f in fleet if f["model"] == "agents-a1")
    bk = sy.get("backups") or {}
    bp, bkk = bk.get("prometheus", {}), bk.get("kanban", {})
    return (
        '<div class="stats-row">'
        + stat("prometheus.db", f'{res["db_mb"]} MB', "live WAL")
        + stat("memory", f'{res["mem_pct"]}%', f'{res["mem_used_gb"]} / {res["mem_total_gb"]} GB' + meter(res["mem_pct"]))
        + stat("disk", f'{res["disk_pct"]}%', f'{res["disk_used_gb"]} / {res["disk_total_gb"]} GB' + meter(res["disk_pct"]))
        + stat("db locks 24h", sy["db_lock_24h"], ">5/day = long-txn writer is back")
        + stat("workers running", _fmt_num(n_running),
               f'{n_a1} on local A1 · router {"ON" if sy.get("a1_router_on") else "OFF"} · :8001 '
               + ("up" if sy.get("a1_up") else "DOWN"))
        + stat("churn (3h)", _fmt_num(sy.get("churn_now", 0)),
               "protocol-violations + gave-ups + crashes; goal-loop fix holds if this stays low")
        + stat("backups", f'{bp.get("n","—")} + {bkk.get("n","—")}',
               f'prom {bp.get("age","—")}{" STALE" if bp.get("stale") else ""} · '
               f'kanban {bkk.get("age","—")}{" STALE" if bkk.get("stale") else ""} · retention 10')
        + "</div>"
        + '<div class="two">'
        + fig("Worker fleet by model", svg_hbar(fleet_pairs) if fleet_pairs else '<div class="empty">no running tasks</div>',
              "running kanban tasks by model override — the free local A1 lane vs the paid fleet default.")
        + fig("Protocol churn, last 24h (3h buckets)", svg_bars(sy.get("churn_series") or [], height=150),
              "worker exits without kanban_complete + reaper give-ups + crashes. "
              "The Jul-7 goal-loop / bounded-reaper / janitor-abandon fixes should hold this near zero.")
        + "</div>"
        + '<div class="two">'
        + panel("Alerts", alerts)
        + panel("Recent log errors", errors)
        + "</div>"
    )


# ═════════════════════════════════════════════════════════════════
# page assembly
# ═════════════════════════════════════════════════════════════════

SECTIONS = [
    ("overview", "Overview", "the loop at a glance"),
    ("gates", "Gates", "retest · attack · arbitration"),
    ("independence", "Independence", "prior-feed stamps + haircut"),
    ("discovery", "Discovery", "the shelf and its knockouts"),
    ("novelty", "Novelty", "literature audits"),
    ("metaprober", "Meta-prober", "self-prediction, reconciled"),
    ("scopes", "Scopes", "regimes + boundary pool"),
    ("claims", "Claims", "lifecycle + calibration"),
    ("domains", "Domains", "where experiments land"),
    ("lineage", "Lineage", "depth + transfer routes"),
    ("activity", "Activity", "the raw feed"),
    ("cron", "Cron", "the metabolism"),
    ("rag", "RAG", "knowledge feed + memory"),
    ("system", "System", "resources + alerts"),
]

RAIL_GROUPS = [("SCIENCE", ["overview", "discovery", "novelty", "domains", "lineage"]),
               ("EPISTEMICS", ["gates", "independence", "metaprober", "scopes", "claims"]),
               ("OPERATIONS", ["activity", "cron", "rag", "system"])]

PLATES = [("prometheus-topology.html", "Topology 2-D"),
          ("prometheus-topology-3d.html", "Topology 3-D"),
          ("prometheus-discoveries.html", "Discoveries")]


def _gather_all():
    """Run every gatherer fault-isolated; a broken one becomes an error card."""
    out = {}
    for name, fn in [
        ("stats", get_stats), ("series", get_series), ("feed", get_feed),
        ("gates", get_gates), ("independence", get_independence),
        ("discovery", get_discovery), ("novelty", get_novelty),
        ("metaprober", get_metaprober), ("scopes", get_scopes),
        ("claims", get_claims_detail), ("domains", get_domains),
        ("lineage", get_lineage), ("cron", get_cron), ("rag", get_rag),
        ("system", get_system),
    ]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001 — fault isolation is the point
            out[name] = None
            out.setdefault("_errors", {})[name] = f"{type(e).__name__}: {e}"
    return out


def _render_section(sid, d):
    errs = d.get("_errors", {})

    def need(*keys):
        missing = [k for k in keys if d.get(k) is None]
        if missing:
            return err_panel(sid, "; ".join(f"{k}: {errs.get(k,'?')}" for k in missing))
        return None
    try:
        if sid == "overview":
            return need("stats", "series", "feed", "gates") or render_overview(d)
        if sid == "gates":
            return need("gates") or render_gates(d["gates"])
        if sid == "independence":
            return need("independence") or render_independence(d["independence"])
        if sid == "discovery":
            return need("discovery") or render_discovery(d["discovery"])
        if sid == "novelty":
            return need("novelty") or render_novelty(d["novelty"])
        if sid == "metaprober":
            return need("metaprober") or render_metaprober(d["metaprober"])
        if sid == "scopes":
            return need("scopes") or render_scopes(d["scopes"])
        if sid == "claims":
            return need("claims") or render_claims(d["claims"])
        if sid == "domains":
            return need("domains") or render_domains(d["domains"])
        if sid == "lineage":
            return need("lineage") or render_lineage(d["lineage"])
        if sid == "activity":
            return need("feed") or render_activity(d["feed"])
        if sid == "cron":
            return need("cron") or render_cron(d["cron"])
        if sid == "rag":
            return need("rag") or render_rag(d["rag"])
        if sid == "system":
            return need("system") or render_system(d["system"])
    except Exception as e:  # noqa: BLE001
        return err_panel(sid, f"render: {type(e).__name__}: {e}")
    return err_panel(sid, "unknown section")


CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
:root {
  --ground:#40e0d0; --panel:#5fe6d9; --panel2:#4de2d4;
  --ink:#04302b; --ink2:#12524a; --faint:#22625a;
  --rule:rgba(4,48,43,.30); --rule2:rgba(4,48,43,.14);
  --accent:#025c52; --bad:#c23a26;
}
html { font-size:15px; }
body { background:var(--ground); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
  line-height:1.45; }
a { color:var(--accent); }
h1,h2,h3,figcaption { font-family:Georgia,'Times New Roman',serif; }
.layout { display:grid; grid-template-columns:230px 1fr; min-height:100vh; }

.rail { border-right:1px solid var(--rule); padding:26px 0 30px; position:sticky; top:0;
  height:100vh; overflow-y:auto; }
.brand { padding:0 22px 18px; border-bottom:1px solid var(--rule2); }
.brand h1 { font-size:1.45rem; letter-spacing:.02em; }
.brand .sub { font-size:.72rem; color:var(--ink2); margin-top:3px; letter-spacing:.06em; }
.rail-group { margin-top:18px; }
.rail-group .gt { font-size:.64rem; letter-spacing:.18em; color:var(--faint); padding:0 22px 6px; }
.rail a.nav { display:block; padding:6px 22px; color:var(--ink); text-decoration:none; font-size:.9rem;
  border-left:3px solid transparent; }
.rail a.nav:hover { background:var(--panel2); }
.rail a.nav.active { background:var(--panel); border-left-color:var(--accent); font-weight:600; }
.rail .plates { margin-top:22px; padding:14px 22px 0; border-top:1px solid var(--rule2); }
.rail .plates a { display:block; font-size:.8rem; padding:3px 0; }
.rail .foot { padding:16px 22px 0; font-size:.7rem; color:var(--faint); }

.main { padding:30px 38px 60px; max-width:1180px; }
.masthead { display:flex; justify-content:space-between; align-items:baseline;
  border-bottom:2px solid var(--rule); padding-bottom:12px; margin-bottom:8px; }
.masthead h2 { font-size:1.7rem; }
.masthead .mh-right { font-size:.78rem; color:var(--ink2); text-align:right; }
.masthead .mh-right select { background:var(--panel2); color:var(--ink); border:1px solid var(--rule);
  border-radius:4px; padding:2px 6px; font-size:.75rem; }
.section-sub { font-size:.85rem; color:var(--ink2); font-style:italic; margin-bottom:20px; }
.section { display:none; }
.section.active { display:block; }

.stats-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin-bottom:18px; }
.stat { background:var(--panel); border:1px solid var(--rule2); border-radius:6px; padding:12px 14px; }
.stat-v { font-size:1.5rem; font-weight:650; font-variant-numeric:tabular-nums; }
.stat-l { font-size:.72rem; letter-spacing:.1em; text-transform:uppercase; color:var(--ink2); margin-top:2px; }
.stat-sub { font-size:.74rem; color:var(--faint); margin-top:4px; }

.panel, figure.panel { background:var(--panel); border:1px solid var(--rule2); border-radius:6px;
  padding:16px 18px; margin-bottom:18px; }
.panel h3 { font-size:1.05rem; margin-bottom:10px; border-bottom:1px solid var(--rule2); padding-bottom:6px; }
figcaption { font-size:.78rem; color:var(--ink2); margin-top:8px; border-top:1px solid var(--rule2); padding-top:6px; }
.fign { font-weight:700; color:var(--accent); }
.pnote { font-size:.78rem; color:var(--ink2); margin-top:8px; }
.two { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
.three { display:grid; grid-template-columns:1fr 1fr 1fr; gap:18px; }
@media (max-width:1100px){ .two,.three { grid-template-columns:1fr; } .layout{grid-template-columns:200px 1fr;} }

.chart { width:100%; height:auto; display:block; }
.chart .clab { font-size:10px; fill:var(--ink2); }
.chart .cnum { font-size:10px; fill:var(--ink); font-weight:600; }
.chart .grid { stroke:var(--rule2); stroke-width:1; }
.donut { width:150px; height:150px; margin:6px auto; display:block; }
.donut .dnum { font-size:20px; font-weight:700; fill:var(--ink); }
.legend { display:flex; flex-wrap:wrap; gap:12px; justify-content:center; font-size:.78rem; margin-top:6px; }
.legend .lg i { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; }
.empty { color:var(--faint); font-size:.85rem; padding:12px 0; text-align:center; }

.feed { display:flex; flex-direction:column; gap:2px; }
.feed.tall { max-height:70vh; overflow-y:auto; }
.feed-row { display:grid; grid-template-columns:64px 150px 1fr auto; gap:10px; align-items:baseline;
  padding:5px 8px 5px 10px; border-left:3px solid var(--rule2); background:var(--panel2);
  border-radius:0 4px 4px 0; font-size:.82rem; }
.feed-row.v-confirmed { border-left-color:#033f38; }
.feed-row.v-supported { border-left-color:#3f5d8c; }
.feed-row.v-partial { border-left-color:#b0783a; }
.feed-row.v-refuted { border-left-color:var(--bad); }
.ft { color:var(--faint); font-variant-numeric:tabular-nums; }
.fd { color:var(--ink2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fh { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.fc { font-variant-numeric:tabular-nums; color:var(--accent); }
.fc.neg { color:var(--bad); }

.badge { display:inline-block; font-size:.68rem; padding:1px 7px; border-radius:9px;
  border:1px solid var(--rule); color:var(--ink2); margin-right:4px; }
.badge.ok { background:var(--accent); color:#d8fff9; border-color:var(--accent); }
.badge.bad, .badge.kill { background:var(--bad); color:#ffe8e2; border-color:var(--bad); }
.disc { border-top:1px solid var(--rule2); padding:9px 0; }
.disc:first-child { border-top:none; }
.disc-head { font-size:.8rem; margin-bottom:3px; }
.disc-text { font-size:.86rem; }
.subblock { background:var(--panel2); border-left:3px solid var(--accent); border-radius:0 4px 4px 0;
  padding:8px 12px; margin:6px 0; font-size:.84rem; }
.subblock.bad { border-left-color:var(--bad); }
.kv { font-size:.92rem; }
.kv b { font-variant-numeric:tabular-nums; }
.warn { color:var(--bad); }
.dim { color:var(--faint); font-weight:400; }
.mono { font-family:ui-monospace,'SF Mono',Menlo,Consolas,monospace; font-size:.78rem; }
.small { font-size:.72rem; }

.armed { border:1px solid var(--rule); border-radius:6px; padding:14px 16px; background:var(--panel2); }
.armed-state { font-family:Georgia,serif; font-size:1.3rem; letter-spacing:.14em; color:var(--bad); }
.armed.on .armed-state { color:var(--accent); }
.armed-detail { font-size:.82rem; color:var(--ink2); margin:6px 0; }
.meter { height:6px; background:rgba(4,48,43,.18); border-radius:3px; overflow:hidden; margin-top:6px; }
.meter span { display:block; height:100%; border-radius:3px; }

.dom-row { display:grid; grid-template-columns:1fr auto auto; gap:10px; padding:4px 2px;
  border-top:1px solid var(--rule2); font-size:.84rem; align-items:baseline; }
.dom-row:first-child { border-top:none; }
.dom-n { font-weight:650; font-variant-numeric:tabular-nums; }
.dom-meta { color:var(--faint); font-size:.74rem; }

.chain { display:flex; flex-direction:column; gap:4px; max-height:46vh; overflow-y:auto; }
.chain-step { display:flex; gap:9px; font-size:.8rem; padding:4px 8px; background:var(--panel2); border-radius:4px; }
.chain-d { color:var(--accent); font-weight:700; min-width:34px; font-variant-numeric:tabular-nums; }

table { width:100%; border-collapse:collapse; font-size:.82rem; }
th { text-align:left; font-size:.68rem; letter-spacing:.12em; text-transform:uppercase; color:var(--ink2);
  border-bottom:1px solid var(--rule); padding:4px 8px; }
td { padding:5px 8px; border-bottom:1px solid var(--rule2); vertical-align:baseline; }
tr.err td { background:rgba(194,58,38,.10); }
tr.dis td { color:var(--faint); }
.errcell { font-size:.72rem; color:var(--bad); max-width:260px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.scrollx { overflow-x:auto; }
"""

JS = """
(function(){
  var KEY='prom3.section', RKEY='prom3.refresh';
  function activate(id, push){
    document.querySelectorAll('.section').forEach(function(s){ s.classList.toggle('active', s.id==='sec-'+id); });
    document.querySelectorAll('a.nav').forEach(function(a){ a.classList.toggle('active', a.dataset.s===id); });
    var meta = document.querySelector('a.nav[data-s="'+id+'"]');
    document.getElementById('mh-title').textContent = meta ? meta.textContent : id;
    document.getElementById('mh-sub').textContent = meta ? (meta.dataset.sub||'') : '';
    try{ localStorage.setItem(KEY, id); }catch(e){}
    if(push) history.replaceState(null,'','#'+id);
    window.scrollTo(0,0);
  }
  document.querySelectorAll('a.nav').forEach(function(a){
    a.addEventListener('click', function(ev){ ev.preventDefault(); activate(a.dataset.s, true); });
  });
  var initial = (location.hash||'').replace('#','');
  if(!initial){ try{ initial = localStorage.getItem(KEY)||''; }catch(e){} }
  if(!document.getElementById('sec-'+initial)) initial='overview';
  activate(initial, false);

  var sel = document.getElementById('refresh-sel'), timer=null;
  function arm(){
    if(timer) clearInterval(timer);
    var s = parseInt(sel.value,10);
    try{ localStorage.setItem(RKEY, sel.value); }catch(e){}
    if(s>0) timer = setInterval(function(){ if(!document.hidden) location.reload(); }, s*1000);
  }
  try{ var saved = localStorage.getItem(RKEY); if(saved!==null) sel.value=saved; }catch(e){}
  if(![].some.call(sel.options,function(o){return o.value===sel.value;})) sel.value='30';
  sel.addEventListener('change', arm);
  arm();
})();
"""


def build_page():
    _FIG["n"] = 0
    d = _gather_all()
    _cache["errors"] = dict(d.get("_errors") or {})   # surfaced by /health
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sub_by_id = {sid: sub for sid, _t, sub in SECTIONS}
    title_by_id = {sid: t for sid, t, _s in SECTIONS}

    rail = []
    for gname, ids in RAIL_GROUPS:
        rail.append(f'<div class="rail-group"><div class="gt">{gname}</div>')
        for sid in ids:
            rail.append(f'<a class="nav" href="#{sid}" data-s="{sid}" data-sub="{_esc(sub_by_id[sid])}">{title_by_id[sid]}</a>')
        rail.append("</div>")
    plates = "".join(f'<a href="/plate/{fn}">{label} ↗</a>' for fn, label in PLATES)

    sections_html = "".join(
        f'<div class="section" id="sec-{sid}">{_render_section(sid, d)}</div>'
        for sid, _t, _s in SECTIONS)

    hb = ""
    st = d.get("stats") or {}
    if st:
        hb = f'{st.get("exp_last_hour","?")}/hr · {st.get("active_workers","?")} workers'

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prometheus — live</title>
<style>{CSS}</style>
</head><body>
<div class="layout">
  <nav class="rail">
    <div class="brand"><h1>Prometheus</h1><div class="sub">AUTONOMOUS RESEARCH · LIVE</div></div>
    {''.join(rail)}
    <div class="plates"><div class="gt">PLATES</div>{plates}</div>
    <div class="foot">built {now_str}<br>{hb}</div>
  </nav>
  <main class="main">
    <div class="masthead">
      <h2 id="mh-title">Overview</h2>
      <div class="mh-right">
        <span id="mh-sub"></span><br>
        refresh <select id="refresh-sel"><option value="0">off</option><option value="15">15s</option>
        <option value="30" selected>30s</option><option value="60">60s</option></select>
      </div>
    </div>
    <div class="section-sub"></div>
    {sections_html}
  </main>
</div>
<script>{JS}</script>
</body></html>"""


# ═════════════════════════════════════════════════════════════════
# server — cached page, whitelisted plates, health json
# ═════════════════════════════════════════════════════════════════

_cache = {"html": None, "ts": 0.0, "build_ms": 0}
_cache_lock = threading.Lock()
_PLATE_WHITELIST = {fn for fn, _ in PLATES}


def cached_page():
    now = time.time()
    if _cache["html"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["html"], _cache["build_ms"], True
    with _cache_lock:
        # re-check under the lock — another thread may have rebuilt
        if _cache["html"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["html"], _cache["build_ms"], True
        t0 = time.time()
        html_out = build_page()
        _cache.update({"html": html_out, "ts": time.time(),
                       "build_ms": int((time.time() - t0) * 1000)})
        return html_out, _cache["build_ms"], False


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                page, build_ms, hit = cached_page()
                self._send(200, page, extra={"X-Build-Ms": str(build_ms),
                                             "X-Cache": "hit" if hit else "miss"})
            elif path == "/health":
                n_errs = len(_cache.get("errors") or {})
                stale = bool(_cache["ts"] and time.time() - _cache["ts"] > CACHE_TTL * 20)
                self._send(200, json.dumps({"ok": n_errs == 0 and not stale,
                                            "gather_errors": n_errs,
                                            "failing_sections": sorted((_cache.get("errors") or {}).keys()),
                                            "ts": time.time(),
                                            "cache_age_s": round(time.time() - _cache["ts"], 1),
                                            "build_ms": _cache["build_ms"]}),
                           ctype="application/json")
            elif path.startswith("/plate/"):
                name = os.path.basename(path[len("/plate/"):])
                if name in _PLATE_WHITELIST:
                    fp = os.path.join(DOCS_DIR, name)
                    if os.path.exists(fp):
                        with open(fp, "rb") as f:
                            self._send(200, f.read())
                        return
                self._send(404, "not found", ctype="text/plain")
            elif path == "/favicon.ico":
                self._send(200, ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                                 '<rect width="16" height="16" rx="3" fill="#40e0d0"/>'
                                 '<circle cx="8" cy="8" r="4" fill="#04302b"/></svg>'),
                           ctype="image/svg+xml")
            else:
                self._send(404, "not found", ctype="text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 — a handler crash must not kill the thread pool
            try:
                self._send(500, f"error: {_esc(e)}", ctype="text/plain")
            except Exception:
                pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(os.environ.get("DASH_PORT", PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Prometheus dashboard v3 (turquoise plate) on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
