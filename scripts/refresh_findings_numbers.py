#!/usr/bin/env python3
"""Print every quantitative claim in FINDINGS.md / LAUNCH_POST.md from live.

On-demand (NOT cron) — run before posting the launch draft to catch number
drift. Read-only; each line notes the file/claim it backs so edits are
mechanical.

    HERMES_HOME=~/.hermes python3 refresh_findings_numbers.py
"""
import json
import os
import sqlite3

HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def _db():
    return sqlite3.connect(f"file:{HOME}/prometheus.db?mode=ro", uri=True)


def _load(name):
    try:
        with open(os.path.join(HOME, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    c = _db()
    q = lambda s: c.execute(s).fetchone()[0]

    experiments = q("SELECT COUNT(*) FROM experiments")
    claims_all = q("SELECT COUNT(*) FROM knowledge_claims")
    claims_live = q("SELECT COUNT(*) FROM knowledge_claims WHERE COALESCE(claim_status,'') != 'MERGED'")
    merged = q("SELECT COUNT(*) FROM knowledge_claims WHERE claim_status = 'MERGED'")
    try:
        shelf = q("SELECT COUNT(*) FROM discovery_candidates")
    except sqlite3.Error:
        shelf = "n/a"

    wc = _load("world_calibration.json")
    holds, fails = wc.get("holds_verified"), wc.get("fails_verified")
    world_n = (holds or 0) + (fails or 0)
    mc = _load("mechanism_calibration.json")
    mono = mc.get("by_mechanism", {}).get("MONOTONIC", {})

    # transfer self-prediction: signed sign-match on resolved meta_transfer rows
    rows = c.execute(
        "SELECT predicted_direction, observed_direction FROM meta_transfer_predictions "
        "WHERE predicted_direction IS NOT NULL AND observed_direction IS NOT NULL "
        "AND outcome IS NOT NULL").fetchall()
    match = n = 0
    for pd, od in rows:
        try:
            pdv = json.loads(pd) if isinstance(pd, str) and pd.strip().startswith("{") else pd
            odv = json.loads(od) if isinstance(od, str) and od.strip().startswith("{") else od
        except (ValueError, TypeError):
            continue
        if isinstance(pdv, dict) and isinstance(odv, dict):
            for k in pdv:
                if k in odv and pdv[k] and odv[k]:
                    n += 1
                    match += 1 if (pdv[k] > 0) == (odv[k] > 0) else 0
        elif pdv is not None and odv is not None:
            try:
                m = (float(pdv) > 0) == (float(odv) > 0)
                n += 1
                match += 1 if m else 0
            except (ValueError, TypeError):
                pass
    transfer_pct = round(100 * match / n) if n else None

    cron = _load_jobs()

    print("=== live numbers for FINDINGS.md / LAUNCH_POST.md ===")
    print(f"experiments                : {experiments:,}   [FINDINGS scale; LAUNCH '~133k']")
    print(f"claims (non-MERGED)        : {claims_live:,}   [FINDINGS scale; LAUNCH '~77k']")
    print(f"claims (all, incl MERGED)  : {claims_all:,}  (merged fragments: {merged:,})")
    print(f"discovery shelf            : {shelf}   [FINDINGS '127 promoted'; LAUNCH shelf ref]")
    print(f"world re-tests resolved    : {wc.get('resolved')}   [FINDINGS '54 world-grounding']")
    print(f"world verified agreement   : {holds}/{world_n} = "
          f"{round(100*(holds or 0)/world_n) if world_n else '?'}%   "
          f"[FINDINGS/LAUNCH '15/21 ≈ 71%']")
    print(f"MONOTONIC over-trust       : {mono.get('confirmation_pct')}% (n={mono.get('n')}), "
          f"overall {mc.get('overall_confirmation_pct')}%   [LAUNCH '68.7%']")
    print(f"transfer self-prediction   : {match}/{n} sign-match = {transfer_pct}%   "
          f"[LAUNCH '58% (~500 self-probes; 53% at N=32)']")
    print(f"cron jobs                  : {cron}   [LAUNCH '85 cron jobs']")


def _load_jobs():
    try:
        with open(os.path.join(HOME, "cron", "jobs.json")) as f:
            j = json.load(f)
        return len(j if isinstance(j, list) else j.get("jobs", []))
    except (OSError, ValueError):
        return "n/a"


if __name__ == "__main__":
    main()
