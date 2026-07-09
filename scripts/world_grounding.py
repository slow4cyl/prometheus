#!/usr/bin/env python3
"""world_grounding.py — the toy-vs-world lane (report-only stage).

Every internal gate (replication, adversarial survival, arbitration, novelty
audit, independence) tests agreement BETWEEN runs of this system. None of them
ask whether a mechanism that is true in every worker-designed simulation
corresponds to anything measurable in the world — BENCH3 grounds lookup
answers, not novel mechanisms. This lane measures that correspondence for the
discovery shelf, one claim at a time:

  ENQUEUE   ([WORLD] card, blind lane — suppress_prior_context) tasks a worker
            to test a shelf claim against REAL EXTERNAL DATA: a published
            dataset / observational catalog / registry, with provenance
            (name + URL/DOI) declared in the finding and the loader code
            preserved in artifacts. Explicitly NOT a self-designed simulation.
            Contract: WORLD_OUTCOME: HOLDS | FAILS | NO_DATASET — NO_DATASET
            is a first-class result (it maps the toy-boundary itself: the
            claim is not about anything currently measurable).

  DETECT    (world_basis) reads the preserved artifact code and classifies its
            data basis: external (network/dataset I/O), synthetic (random
            generation only), mixed, or no_code. Cross-checks the worker's
            declared provenance mechanically — a HOLDS whose artifact never
            loads external data is counted as unverified, not as a HOLDS
            (declared provenance + detector, per operator decision).

  RECONCILE joins the ledger to completed tasks/results, parses WORLD_OUTCOME
            + DATASET lines, runs the detector over artifacts, and writes
            ~/.hermes/world_calibration.json — the headline number being:
            when this system's simulations confirm a mechanism, how often
            does matched real-world data agree? (verified HOLDS /
            (verified HOLDS + FAILS), with NO_DATASET share alongside.)

REPORT-ONLY BY DESIGN (operator decision 2026-07-04): the ledger and the
calibration number bind nothing — no maturity signal, no spotlight term —
until ~20 resolved outcomes exist and the verification pipeline has held up.
Note the results themselves still flow through normal intake as ordinary
evidence (the card re-embeds HYPOTHESIS:, so a FAILS is a real refutation and
disputes the claim through the standard contradiction basis — that is the
evidence machinery working, not this lane gating).

Scope: discovery-route shelf claims only (~50). Cap 3 live, 2/run.

Usage:
  world_grounding.py                     # reconcile + dry-print targets
  world_grounding.py --enqueue --limit 2 # enqueue [WORLD] cards (cron mode)
  world_grounding.py --scan              # detector sweep: shelf sim-share
  world_grounding.py --reconcile         # reconcile outcomes + write json
"""

import argparse
from prometheus_paths import KANBAN_DB as _PP_KANBAN_DB, PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import re
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = _PP_PROMETHEUS_DB
KANBAN = _PP_KANBAN_DB
ARTIFACTS = os.path.expanduser("~/.hermes/artifacts")
CALIB = os.path.expanduser("~/.hermes/world_calibration.json")

WORLD_MODEL = os.environ.get("HERMES_WORLD_MODEL", "deepseek/deepseek-v4-flash")
MAX_PENDING = 3

OUTCOME_RE = re.compile(r"WORLD_OUTCOME:\s*(HOLDS|FAILS|NO_DATASET)", re.I)
DATASET_RE = re.compile(r"DATASET:\s*(.+)", re.I)

# ---------------------------------------------------------------------------
# world_basis detector — classify an experiment's data basis from its
# preserved artifact code. Mechanical, conservative: external I/O anywhere
# beats synthetic markers (real analyses also use np.random for CV splits).
# ---------------------------------------------------------------------------
_EXTERNAL_PAT = re.compile(
    r"requests\.|urllib\.request|urlopen|wget |curl |astroquery|yfinance"
    r"|fetch_openml|fetch_california|fetch_20news|load_dataset\s*\("
    r"|datasets\.load|kagglehub|read_csv\s*\(\s*['\"]https?://"
    r"|read_json\s*\(\s*['\"]https?://|huggingface_hub|hf_hub_download"
    r"|Vizier|SDSS|MAST|GWOSC|fredapi|pandas_datareader|zenodo|figshare"
    r"|api\.crossref|api\.semanticscholar|pooch\.", re.I)
_SYNTH_PAT = re.compile(
    r"np\.random\.|numpy\.random|torch\.rand|torch\.randn|random\.gauss"
    r"|make_classification|make_regression|make_blobs|make_moons"
    r"|rng\s*=\s*np\.random|default_rng", re.I)
# reading a local file the script did not itself write is weak-external
_LOCAL_READ_PAT = re.compile(r"read_csv\s*\(|np\.loadtxt|open\s*\([^)]*['\"]r['\"]", re.I)


def world_basis(task_id):
    """-> (basis, evidence) where basis in external|mixed|synthetic|local_file|no_code."""
    d = os.path.join(ARTIFACTS, str(task_id))
    if not os.path.isdir(d):
        return "no_code", "no artifact dir"
    code = ""
    for fn in sorted(os.listdir(d)):
        if fn.endswith((".py", ".sh", ".ipynb", ".r", ".jl")):
            try:
                with open(os.path.join(d, fn), errors="replace") as f:
                    code += f.read() + "\n"
            except OSError:
                pass
    if not code.strip():
        return "no_code", "no code files in artifacts"
    ext = bool(_EXTERNAL_PAT.search(code))
    syn = bool(_SYNTH_PAT.search(code))
    loc = bool(_LOCAL_READ_PAT.search(code))
    if ext and syn:
        return "mixed", "external I/O + synthetic generation"
    if ext:
        return "external", _EXTERNAL_PAT.search(code).group(0)
    if loc and not syn:
        return "local_file", "reads local files, no synthetic markers"
    if syn:
        return "synthetic", _SYNTH_PAT.search(code).group(0)
    return "synthetic", "no data I/O detected"


# ---------------------------------------------------------------------------
# lane plumbing
# ---------------------------------------------------------------------------

def rw():
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_ledger(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS world_groundings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id INTEGER NOT NULL,
        kanban_task_id TEXT,
        experiment_id TEXT,
        model TEXT,
        created_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',   -- pending|resolved|dead
        outcome TEXT,                             -- HOLDS|FAILS|NO_DATASET
        dataset TEXT,
        basis TEXT,                               -- world_basis() verdict
        verified INTEGER,                         -- 1 = outcome basis checks out
        resolved_at REAL)""")
    conn.commit()


def get_targets(conn):
    """Discovery-route shelf claims, most claim-support first, that have no
    live [WORLD] task and no resolved HOLDS/FAILS outcome. NO_DATASET stays
    terminal-for-now (visible in the ledger; re-open by hand if a dataset
    appears). Live tier membership re-verified against knowledge_claims."""
    return conn.execute("""
        SELECT dc.claim_id, kc.claim_status AS tier, kc.domain,
               kc.hypothesis_text, kc.claim_summary,
               COALESCE(kc.weighted_support_count,0) AS wsc
        FROM discovery_candidates dc
        JOIN knowledge_claims kc ON kc.id = dc.claim_id
        WHERE dc.route = 'discovery'
          AND kc.claim_status IN ('REPLICATED','ESTABLISHED')
          AND COALESCE(kc.is_meta,0)=0 AND COALESCE(kc.is_empirical_fact,0)=0
          AND NOT EXISTS (SELECT 1 FROM world_groundings wg
                          WHERE wg.claim_id = dc.claim_id
                            AND wg.status IN ('pending','resolved')
                            AND COALESCE(wg.outcome,'') != '')
          AND NOT EXISTS (SELECT 1 FROM world_groundings wg2
                          WHERE wg2.claim_id = dc.claim_id AND wg2.status='pending')
        ORDER BY wsc DESC
    """).fetchall()


def make_body(exp_id, t):
    finding = (t["claim_summary"] or "").strip()
    hyp = (t["hypothesis_text"] or "").strip()
    return f"""[WORLD] External-data grounding. Report-only measurement lane.

HYPOTHESIS: {hyp}

CLAIMED FINDING (established inside this system, simulations only):
{finding[:900]}

This claim has survived replication and adversarial attack — but every test so
far ran on data the workers generated themselves. Your job is to test it
against the WORLD: real, external, published data that neither you nor any
prior worker constructed.

RULES:
1. Find a REAL dataset that can test the core quantitative content of this
   claim — a published dataset, observational catalog, benchmark, or registry
   (OpenML, UCI, HuggingFace datasets, Zenodo, GWOSC, SDSS/MAST, FRED, WHO,
   government open data, a paper's public supplement...). Web search is
   encouraged for FINDING data; the TEST itself must run on the data.
2. Do NOT construct a simulation and call it data. Generating the dataset
   yourself (np.random, make_classification, agent-based sims) = NOT external.
   Preprocessing real data is fine; synthesizing it is not. Your preserved
   code is mechanically audited for external I/O vs synthetic generation —
   a HOLDS with no external data loading in the artifacts is discarded.
3. If after genuine search no real dataset can test this claim, that IS the
   result: report NO_DATASET honestly and say what measurement would be
   needed. Discovering the claim is untestable-in-the-world is exactly what
   this lane exists to map. Do not force a bad proxy dataset.

PREREGISTER (MANDATORY, before loading any data): PREDICTION: HOLDS or FAILS,
CONFIDENCE 0.0-1.0, one WHY line. Report via
--predicted-direction '{{"world_holds": +1|-1}}' and the measured sign in
--observed-direction on the same key.

REPORT (MANDATORY lines inside your finding text):
  WORLD_OUTCOME: HOLDS | FAILS | NO_DATASET
  DATASET: <name> — <URL or DOI>   (omit only for NO_DATASET)

RESULT WRITING (run BEFORE kanban_complete):
  python3 ~/.hermes/scripts/write_worker_result.py --experiment {exp_id} \\
    --finding "CONFIRMED/REFUTED: ... WORLD_OUTCOME: ... DATASET: ..." \\
    [--supported] --confidence 0.XX --domain {t['domain'] or 'auto'} \\
    --basis independent_computation \\
    --predicted-direction '{{"world_holds": 1}}' \\
    --observed-direction '{{"world_holds": <+1 or -1>}}' \\
    --files "world_test.py,results.json"
  (--supported iff WORLD_OUTCOME: HOLDS. Confidence hard-capped at 0.85.
   List your data-loading script in --files — the preservation is the audit.)
"""


def enqueue(conn, limit, dry):
    ensure_ledger(conn)
    targets = get_targets(conn)
    pending = conn.execute(
        "SELECT COUNT(*) FROM world_groundings WHERE status='pending'").fetchone()[0]
    slots = max(0, min(limit, MAX_PENDING - pending))
    print(f"[WORLD] targets={len(targets)} pending={pending} slots={slots}")
    created = 0
    for t in targets[:slots] if not dry else targets[:limit]:
        if dry:
            print(f"  would enqueue #{t['claim_id']} ({t['tier']}, wsc={t['wsc']:.1f}, "
                  f"[{t['domain']}]) {(t['claim_summary'] or t['hypothesis_text'] or '')[:70]}")
            continue
        exp_id = f"exp_world{t['claim_id']}_{uuid.uuid4().hex[:4]}"
        title = (f"{exp_id}: [WORLD] Ground claim #{t['claim_id']} in external data: "
                 f"{(t['hypothesis_text'] or '')[:55]}")
        body = make_body(exp_id, t)
        tid = "t_" + uuid.uuid4().hex[:8]
        try:
            k = sqlite3.connect(KANBAN, timeout=15)
            k.execute("PRAGMA busy_timeout=15000")
            k.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, priority, "
                "model_override, created_at) VALUES (?,?,?,?, 'ready', 2, ?, ?)",
                (tid, title, body, "default", WORLD_MODEL, int(time.time())))
            k.commit()
            k.close()
        except Exception as e:  # noqa: BLE001
            print(f"  create failed for #{t['claim_id']}: {e}")
            continue
        try:
            from prior_feed_stamp import record as _stamp
            _stamp(tid, body)
        except Exception:
            pass
        conn.execute(
            "INSERT INTO world_groundings (claim_id, kanban_task_id, experiment_id, "
            "model, created_at) VALUES (?,?,?,?,?)",
            (t["claim_id"], tid, exp_id, WORLD_MODEL, time.time()))
        conn.commit()
        created += 1
        print(f"  ENQUEUED world-grounding of #{t['claim_id']} -> {tid} ({exp_id})")
    if not dry:
        print(f"  enqueued {created}")


def reconcile(conn):
    """Close out completed [WORLD] tasks: parse outcome + dataset, audit the
    artifact code, mark dead tasks, refresh world_calibration.json."""
    ensure_ledger(conn)
    k = sqlite3.connect(f"file:{KANBAN}?mode=ro", uri=True, timeout=15)
    resolved = dead = 0
    for wg in conn.execute(
            "SELECT * FROM world_groundings WHERE status='pending'").fetchall():
        st = k.execute("SELECT status FROM tasks WHERE id=?",
                       (wg["kanban_task_id"],)).fetchone()
        kstatus = st[0] if st else (
            "archived" if k.execute("SELECT 1 FROM archived_tasks WHERE id=?",
                                    (wg["kanban_task_id"],)).fetchone() else "gone")
        if kstatus in ("ready", "running", "blocked"):
            continue
        wr = conn.execute(
            "SELECT COALESCE(key_finding, finding) AS result FROM worker_results "
            "WHERE experiment_id LIKE ? "
            "ORDER BY id DESC LIMIT 1", (wg["experiment_id"] + "%",)).fetchone()
        if not wr or not (wr["result"] or "").strip():
            # completed/archived task with no result — dead attempt, claim re-eligible
            conn.execute("UPDATE world_groundings SET status='dead', resolved_at=? "
                         "WHERE id=?", (time.time(), wg["id"]))
            dead += 1
            continue
        text = wr["result"]
        m = OUTCOME_RE.search(text)
        outcome = m.group(1).upper() if m else None
        dm = DATASET_RE.search(text)
        dataset = dm.group(1).strip()[:300] if dm else None
        basis, ev = world_basis(wg["kanban_task_id"])
        # verification (declared provenance + detector): a HOLDS/FAILS needs
        # external (or at least local-file/mixed) I/O in the preserved code AND
        # a declared dataset. NO_DATASET needs neither.
        if outcome in ("HOLDS", "FAILS"):
            verified = 1 if (dataset and basis in ("external", "mixed", "local_file")) else 0
        elif outcome == "NO_DATASET":
            verified = 1
        else:
            verified = 0
        conn.execute(
            "UPDATE world_groundings SET status='resolved', outcome=?, dataset=?, "
            "basis=?, verified=?, resolved_at=? WHERE id=?",
            (outcome, dataset, basis, verified, time.time(), wg["id"]))
        resolved += 1
        print(f"  resolved #{wg['claim_id']}: {outcome or 'NO OUTCOME LINE'} "
              f"basis={basis} verified={verified}"
              + (f" dataset={dataset[:60]}" if dataset else ""))
    conn.commit()
    k.close()

    rows = conn.execute("SELECT outcome, verified, COUNT(*) c FROM world_groundings "
                        "WHERE status='resolved' GROUP BY outcome, verified").fetchall()
    holds_v = sum(r["c"] for r in rows if r["outcome"] == "HOLDS" and r["verified"])
    fails_v = sum(r["c"] for r in rows if r["outcome"] == "FAILS" and r["verified"])
    nodata = sum(r["c"] for r in rows if r["outcome"] == "NO_DATASET")
    unver = sum(r["c"] for r in rows if r["outcome"] in ("HOLDS", "FAILS") and not r["verified"])
    total = sum(r["c"] for r in rows)
    rate = round(holds_v / (holds_v + fails_v), 3) if (holds_v + fails_v) else None
    snap = {"updated_at": time.time(), "resolved": total,
            "holds_verified": holds_v, "fails_verified": fails_v,
            "no_dataset": nodata, "unverified_outcomes": unver,
            "world_agreement_rate": rate,
            "note": "verified HOLDS / (verified HOLDS + verified FAILS); report-only, "
                    "binds nothing until ~20 outcomes and verification holds up"}
    with open(CALIB + ".tmp", "w") as f:
        json.dump(snap, f, indent=1)
    os.replace(CALIB + ".tmp", CALIB)
    if resolved or dead:
        print(f"  reconcile: {resolved} resolved, {dead} dead")
    print(f"  world agreement: {rate if rate is not None else 'n/a'} "
          f"(HOLDS {holds_v} / FAILS {fails_v} / NO_DATASET {nodata} / unverified {unver})")


def scan_shelf(conn):
    """Detector sweep over the shelf's EXISTING support experiments: how much
    of the discovery shelf rests on synthetic-only code?"""
    rows = conn.execute("""
        SELECT dc.claim_id, wr.kanban_task_id
        FROM discovery_candidates dc
        JOIN claim_evidence ce ON ce.claim_id = dc.claim_id
        JOIN worker_results wr ON wr.id = ce.worker_result_id
        WHERE dc.route='discovery' AND wr.hypothesis_supported=1
          AND COALESCE(ce.evidence_type,'support') != 'retracted_by_arbitration'
          AND wr.kanban_task_id IS NOT NULL
    """).fetchall()
    per_claim = {}
    for r in rows:
        b, _ = world_basis(r["kanban_task_id"])
        per_claim.setdefault(r["claim_id"], []).append(b)
    counts = {"external": 0, "mixed": 0, "local_file": 0, "synthetic": 0, "no_code": 0}
    for cid, bases in sorted(per_claim.items()):
        best = ("external" if "external" in bases else
                "mixed" if "mixed" in bases else
                "local_file" if "local_file" in bases else
                "synthetic" if "synthetic" in bases else "no_code")
        counts[best] += 1
        print(f"  #{cid}: {best}  ({len(bases)} audited supports: "
              f"{', '.join(sorted(set(bases)))})")
    n = len(per_claim)
    print(f"\nshelf world-basis (best support per claim, n={n}): {counts}")
    if n:
        sim_share = (counts["synthetic"] + counts["no_code"]) / n
        print(f"sim-share (synthetic+no_code): {sim_share:.0%} — these ground first")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--enqueue", action="store_true")
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--limit", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = rw()
    ensure_ledger(conn)
    if args.scan:
        scan_shelf(conn)
        return 0
    # cron mode runs reconcile-then-enqueue each pass; default (no flags) is
    # reconcile + dry-print targets.
    reconcile(conn)
    if args.enqueue:
        enqueue(conn, args.limit, dry=args.dry_run)
    else:
        enqueue(conn, args.limit, dry=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
