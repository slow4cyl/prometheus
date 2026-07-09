#!/usr/bin/env python3
"""
evidence_integrity_monitor.py — runs every 30m via cron.
Checks evidence quality without modifying anything.
Output is consumed by the inspector cron for analysis.

Checks:
  1. New duplicate key_findings since last run
  2. REPLICATED claims with n_independent_retests = 0 (regression detector)
  3. ESTABLISHED claims with broken evidence chains
  4. is_cross_domain rate drift
  5. tfidf domain re-entry (METHODOLOGY_LABELS regression)

Writes a JSON status file; inspector reads it. Exits non-zero if any alert fires.
"""

import sqlite3
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import sys
from datetime import datetime, timezone

DB_PATH = _PP_PROMETHEUS_DB
STATE_PATH = os.path.expanduser("~/.hermes/evidence_integrity_state.json")
REPORT_PATH = os.path.expanduser("~/.hermes/evidence_integrity_report.json")

ALERTS = []
METRICS = {}

def alert(msg, data=None):
    ALERTS.append({"message": msg, "data": data})
    print(f"[ALERT] {msg}")

def info(key, val):
    METRICS[key] = val
    print(f"[INFO]  {key}: {val}")


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # --- 1. Duplicate key_findings (new since last run) ---
    load_state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            load_state = json.load(f)

    last_rowid = load_state.get("last_claim_evidence_rowid", 0)

    dupes = conn.execute("""
        SELECT claim_id, key_finding, COUNT(*) as n
        FROM claim_evidence
        WHERE rowid > ?
        GROUP BY claim_id, key_finding
        HAVING n > 1
    """, (last_rowid,)).fetchall()

    info("new_duplicate_key_findings", len(dupes))
    if dupes:
        alert(
            f"{len(dupes)} duplicate key_findings found in new evidence rows",
            [{"claim_id": r["claim_id"], "n": r["n"], "finding": (r["key_finding"] or "")[:120]} for r in dupes[:5]]
        )

    max_rowid = conn.execute("SELECT MAX(rowid) FROM claim_evidence").fetchone()[0] or 0

    # --- 2. REPLICATED claims with no independent retests ---
    bad_replicated = conn.execute("""
        SELECT COUNT(*) as n FROM knowledge_claims
        WHERE claim_status = 'REPLICATED' AND COALESCE(n_independent_retests, 0) = 0
    """).fetchone()["n"]

    info("replicated_without_retests", bad_replicated)
    if bad_replicated > 0:
        samples = [dict(r) for r in conn.execute("""
            SELECT id, substr(hypothesis_text,1,80) as hyp FROM knowledge_claims
            WHERE claim_status = 'REPLICATED' AND COALESCE(n_independent_retests, 0) = 0
            LIMIT 5
        """).fetchall()]
        alert(f"{bad_replicated} REPLICATED claims have n_independent_retests=0 — retest gate regression", samples)

    # --- 3. ESTABLISHED claims: broken evidence chains ---
    broken_established = conn.execute("""
        SELECT COUNT(DISTINCT kc.id) as n
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'ESTABLISHED'
          AND NOT EXISTS (
              SELECT 1 FROM claim_evidence ce
              WHERE ce.claim_id = kc.id
          )
    """).fetchone()["n"]

    info("established_with_no_evidence_links", broken_established)
    if broken_established > 0:
        alert(f"{broken_established} ESTABLISHED claims have zero linked evidence rows")

    # --- 4. is_cross_domain rate drift ---
    xd = conn.execute("""
        SELECT
            ROUND(100.0 * SUM(is_cross_domain) / COUNT(*), 2) as rate,
            COUNT(*) as total
        FROM claim_evidence
    """).fetchone()

    xd_rate = xd["rate"]
    info("is_cross_domain_rate_pct", xd_rate)

    prev_rate = load_state.get("is_cross_domain_rate_pct", xd_rate)
    drift = abs(xd_rate - prev_rate)
    info("is_cross_domain_rate_drift_pp", round(drift, 2))
    if drift > 2.0:
        alert(f"is_cross_domain rate changed by {drift:.1f}pp since last run ({prev_rate}% -> {xd_rate}%)")

    # --- 5. tfidf methodology label re-entry ---
    tfidf_count = conn.execute(
        "SELECT COUNT(*) as n FROM experiments WHERE domain='tfidf'"
    ).fetchone()["n"]

    prev_tfidf = load_state.get("tfidf_experiment_count", tfidf_count)
    info("tfidf_experiment_count", tfidf_count)
    if tfidf_count > prev_tfidf:
        alert(
            f"tfidf experiment count grew by {tfidf_count - prev_tfidf} "
            f"({prev_tfidf} -> {tfidf_count}). METHODOLOGY_LABELS guard may have regressed."
        )

    # --- Write state for next run ---
    new_state = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "last_claim_evidence_rowid": max_rowid,
        "is_cross_domain_rate_pct": xd_rate,
        "tfidf_experiment_count": tfidf_count,
    }
    with open(STATE_PATH, "w") as f:
        json.dump(new_state, f, indent=2)

    # --- Write report for inspector ---
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "alert_count": len(ALERTS),
        "alerts": ALERTS,
        "metrics": METRICS,
    }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    if ALERTS:
        print(f"\n{len(ALERTS)} alert(s) written to {REPORT_PATH}")
        sys.exit(1)
    else:
        print(f"Clean. Report: {REPORT_PATH}")
        sys.exit(0)


if __name__ == "__main__":
    main()
