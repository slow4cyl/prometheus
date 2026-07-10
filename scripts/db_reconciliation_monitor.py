#!/usr/bin/env python3
"""
Reconciliation monitor for prometheus.db <-> kanban.db.

Standing version of the 2026-07-01 backfill audit: catches the failure
classes that let ~580 experiment findings silently accumulate outside the
knowledge store (see docs/architecture-changelog.md, 2026-07-01).

Checks:
  1. lost_experiments      — worker_results marked applied=1 with no experiments
                             row, from BEFORE the baseline. This count can only
                             legitimately shrink; growth means experiments rows
                             were deleted (the June-corruption failure class).
                             (Post-baseline orphans are normal quality-gate
                             rejections and are reported as info, not alerts.)
  2. missing_kanban_results — kanban experiment tasks completed >24h ago (post-
                             baseline) whose run summary never reached prometheus
                             (no worker_results / experiments / synthesis_outputs
                             record). A task whose preserved-artifact experiment_id
                             IS present in prometheus is a BENIGN worker_results
                             UNIQUE(experiment_id) dedup collision (duplicate probes
                             of one claim share an exp_id) — the finding landed,
                             only the task-id backlink was lost; reported as info,
                             not an alert.
  3. stale_synthesis_outputs — synthesis tasks that DID write synthesis_outputs but
                             have been unapplied >24h (merger failure, not result
                             loss).
  4. kanban_strays          — worker_results rows written into kanban.db (wrong
                             DB) that exist nowhere in prometheus after 24h.
  5. empty_result_experiments — experiments >24h old with no result text and no
                             EMPTY_UNRECOVERABLE tag.
  6. null_id_experiments    — rows with NULL/empty id (also blocked by trigger
                             trg_experiments_require_id; this catches UPDATEs).
  7. text_timestamps        — created_at/completed_at stored as text.

Conventions (match sibling monitors): silent + exit 0 when healthy; on alert,
print one line per problem and exit 1. Always writes
~/.hermes/db_reconciliation_report.json.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from prometheus_paths import ARTIFACTS_DIR, KANBAN_DB, PROMETHEUS_DB, under_home

PROM = Path(PROMETHEUS_DB)
KANBAN = Path(KANBAN_DB)
REPORT = Path(under_home("db_reconciliation_report.json"))
ARTIFACTS = Path(ARTIFACTS_DIR)

# Frozen at deployment (2026-07-01, right after the backfill). Orphaned
# worker_results existing at that moment are the 275 known quality-gate
# rejects; anything pushing the pre-baseline count ABOVE this means
# experiments rows have been deleted since.
BASELINE_EPOCH = 1782946841
BASELINE_ORPHANS = 275

GRACE_SECONDS = 24 * 3600
EXP_ID_RE = re.compile(r"(exp_[A-Za-z0-9_]+)")


def ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    return conn


def is_experiment_task(title, assignee):
    t = (title or "").lower()
    return ((assignee or "").startswith("prometheus") or "experiment" in t
            or t.startswith("exp") or "hypothesis" in t or "[exp]" in t)


def artifact_experiment_id(tid):
    """Resolve the prometheus experiment_id a completed task produced, from its
    preserved artifacts (manifest.json's experiment_id, else an exp_*.py/_results
    filename). Returns None when there is no artifact dir or it is unresolvable.

    Purpose: recognise the BENIGN case where a task's finding DID reach prometheus
    under its experiment_id, but the task-id backlink was dropped by the
    worker_results UNIQUE(experiment_id) dedup. Concurrent probes of the same
    claim generate the SAME exp_id (workers name experiments after the claim,
    e.g. exp_67218 / exp_litresidue_67218), so only one row survives per id and it
    may carry a sibling task's kanban_task_id. The title-regex path below never
    catches these because [LIT-RESIDUE]/refiller titles carry 'Claim #N', not an
    exp_ token. Without this, benign dedup collisions false-alarm as lost findings."""
    d = ARTIFACTS / tid
    mf = d / "manifest.json"
    if mf.exists():
        try:
            eid = json.loads(mf.read_text()).get("experiment_id")
            if eid:
                return eid
        except Exception:
            pass
    if d.is_dir():
        for f in sorted(d.glob("exp_*")):
            m = re.match(r"(exp_[A-Za-z0-9_]+?)(_results)?\.(py|json)$", f.name)
            if m:
                return m.group(1)
    return None


def main():
    now = time.time()
    p = ro(PROM)
    k = ro(KANBAN)
    alerts = []
    info = {}

    # --- 1. lost experiments (pre-baseline orphan growth) ---------------------
    pre_orphans = p.execute(
        "SELECT COUNT(*) FROM worker_results w WHERE w.applied=1 "
        "AND NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id=w.experiment_id) "
        "AND (w.created_at IS NULL OR w.created_at <= ?)", (BASELINE_EPOCH,)).fetchone()[0]
    post_orphans = p.execute(
        "SELECT COUNT(*) FROM worker_results w WHERE w.applied=1 "
        "AND NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id=w.experiment_id) "
        "AND w.created_at > ?", (BASELINE_EPOCH,)).fetchone()[0]
    info["pre_baseline_orphans"] = pre_orphans
    info["post_baseline_orphans_gate_rejects"] = post_orphans
    if pre_orphans > BASELINE_ORPHANS:
        alerts.append(f"lost_experiments: pre-baseline orphaned worker_results grew "
                      f"{BASELINE_ORPHANS} -> {pre_orphans}; experiments rows were deleted")

    # --- coverage sets for checks 2 and 3 -------------------------------------
    covered_ktids = {r[0] for r in p.execute(
        "SELECT kanban_task_id FROM worker_results WHERE kanban_task_id IS NOT NULL AND kanban_task_id!='' "
        "UNION SELECT kanban_task_id FROM experiments WHERE kanban_task_id IS NOT NULL AND kanban_task_id!=''")}
    exp_ids = {r[0] for r in p.execute("SELECT id FROM experiments")}
    pwr_ids = {r[0] for r in p.execute("SELECT experiment_id FROM worker_results")}

    # Synthesis tasks are a separate, legitimate output path: they write to
    # synthesis_outputs and are later merged into curiosities. Count that as
    # coverage for "did the task reach prometheus", but keep a separate alert
    # for stale unapplied rows so merger failures are not hidden.
    synthesis_task_ids = set()
    stale_synthesis_outputs = []
    stale_synthesis_count = 0
    try:
        so_cols = {row[1] for row in p.execute("PRAGMA table_info(synthesis_outputs)").fetchall()}
        if "synthesis_task_id" in so_cols:
            synthesis_task_ids = {r[0] for r in p.execute(
                "SELECT DISTINCT synthesis_task_id FROM synthesis_outputs "
                "WHERE synthesis_task_id IS NOT NULL AND synthesis_task_id!=''")}
        if {"id", "synthesis_task_id", "applied", "created_at"}.issubset(so_cols):
            stale_where = ("FROM synthesis_outputs WHERE COALESCE(applied, 0)=0 "
                           "AND typeof(created_at) IN ('real','integer') AND created_at < ?")
            stale_synthesis_count = p.execute(
                f"SELECT COUNT(*) {stale_where}", (now - GRACE_SECONDS,)).fetchone()[0]
            stale_synthesis_outputs = [dict(r) for r in p.execute(
                f"SELECT id, synthesis_task_id, created_at {stale_where} "
                "ORDER BY created_at LIMIT 20", (now - GRACE_SECONDS,))]
    except sqlite3.Error as e:
        alerts.append(f"synthesis_outputs_check_error: {e}")
    info["stale_synthesis_outputs"] = stale_synthesis_outputs
    if stale_synthesis_count:
        alerts.append(f"stale_synthesis_outputs: {stale_synthesis_count} synthesis rows "
                      f"unapplied >24h (e.g. {stale_synthesis_outputs[:3]})")

    # --- 2. completed kanban experiment tasks missing from prometheus ---------
    missing = []
    covered_synthesis_outputs = []
    benign_collisions = []  # result landed under exp_id; task-id backlink lost to dedup
    design_children = []  # decompose "Design ..." planning children — produce a spec, not a result
    writer_utility_tasks = []  # "Write experiment result ..." tasks — result lands under the target experiment
    for t in k.execute(
            "SELECT id, title, assignee, completed_at FROM tasks "
            "WHERE status IN ('done','archived') AND created_at > ? "
            "AND COALESCE(completed_at, created_at) < ?",
            (BASELINE_EPOCH, now - GRACE_SECONDS)):
        if not is_experiment_task(t["title"], t["assignee"]) or t["id"] in covered_ktids:
            continue
        if t["id"] in synthesis_task_ids:
            covered_synthesis_outputs.append(t["id"])
            continue
        m = EXP_ID_RE.search(t["title"] or "")
        if m and m.group(1).rstrip("_") in exp_ids:
            continue
        # Benign experiment_id collision: the finding reached prometheus under its
        # experiment_id (present in experiments or worker_results), but the task-id
        # backlink was dropped by the worker_results UNIQUE(experiment_id) dedup —
        # duplicate probes of one claim share an exp_id. Knowledge landed; only the
        # kanban_task_id provenance was lost. Report as info, not a loss alert.
        aeid = artifact_experiment_id(t["id"])
        if aeid and (aeid in exp_ids or aeid in pwr_ids):
            benign_collisions.append(t["id"])
            continue
        # Design-decomposition child: the auto-decomposer splits a task into a
        # "Design ..." planning child (produces an experiment SPEC for a sibling
        # to execute) plus the executing child. The design child completes with a
        # summary but writes NO worker_result by construction — it is not a lost
        # experiment. Identify by the from_decompose_of created-event marker + a
        # "design"-led title; the executing sibling carries the real result.
        if (t["title"] or "").lower().startswith("design") and k.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind='created' "
                "AND payload LIKE '%from_decompose_of%' LIMIT 1", (t["id"],)).fetchone():
            design_children.append(t["id"])
            continue
        # Writer-utility task: its whole job is invoking write_worker_result.py
        # for a DIFFERENT experiment (title "Write experiment result ...");
        # the result correctly lands under that experiment's id, so this task
        # having no worker_result of its own is by construction (first seen:
        # t_ab15f9c2, whose write landed under exp_1782685474000007426).
        if (t["title"] or "").lower().startswith("write experiment result"):
            writer_utility_tasks.append(t["id"])
            continue
        has_summary = k.execute(
            "SELECT 1 FROM task_runs WHERE task_id=? AND outcome='completed' "
            "AND summary IS NOT NULL AND TRIM(summary)!='' LIMIT 1", (t["id"],)).fetchone()
        if has_summary:
            missing.append(t["id"])
    info["missing_kanban_results"] = missing
    info["covered_synthesis_outputs"] = covered_synthesis_outputs
    info["benign_experiment_id_collisions"] = benign_collisions
    info["design_decomposition_children"] = design_children
    info["writer_utility_tasks"] = writer_utility_tasks
    if missing:
        alerts.append(f"missing_kanban_results: {len(missing)} completed experiment tasks "
                      f"with summaries never reached prometheus (e.g. {missing[:3]})")

    # --- 3. worker_results stranded in kanban.db ------------------------------
    strays = [r[0] for r in k.execute(
        "SELECT experiment_id FROM worker_results WHERE created_at > ? AND created_at < ?",
        (BASELINE_EPOCH, now - GRACE_SECONDS))
        if r[0] and r[0] not in pwr_ids and r[0] not in exp_ids]
    info["kanban_strays"] = strays
    if strays:
        alerts.append(f"kanban_strays: {len(strays)} worker_results written to kanban.db "
                      f"exist nowhere in prometheus (e.g. {strays[:3]})")

    # --- 4. empty-result experiments (untagged) --------------------------------
    empty = p.execute(
        "SELECT COUNT(*) FROM experiments WHERE (result IS NULL OR TRIM(result)='') "
        "AND COALESCE(quality_tags,'') NOT LIKE '%EMPTY_UNRECOVERABLE%' "
        "AND (typeof(created_at) IN ('real','integer') AND created_at < ?)",
        (now - GRACE_SECONDS,)).fetchone()[0]
    info["untagged_empty_result_experiments"] = empty
    if empty:
        alerts.append(f"empty_result_experiments: {empty} experiments >24h old with no "
                      f"result text and no EMPTY_UNRECOVERABLE tag")

    # --- 5. NULL/empty ids -----------------------------------------------------
    nullid = p.execute(
        "SELECT COUNT(*) FROM experiments WHERE id IS NULL OR TRIM(id)=''").fetchone()[0]
    info["null_id_experiments"] = nullid
    if nullid:
        alerts.append(f"null_id_experiments: {nullid} experiments rows with NULL/empty id")

    # --- 6. text timestamps ----------------------------------------------------
    textts = p.execute(
        "SELECT COUNT(*) FROM experiments WHERE typeof(created_at)='text' "
        "OR typeof(completed_at)='text'").fetchone()[0]
    info["text_timestamp_experiments"] = textts
    if textts:
        alerts.append(f"text_timestamps: {textts} experiments rows with ISO-text timestamps")

    # --- 7. claim-fragment recurrence tripwire ----------------------------------
    # normalize_hypothesis identity is fixed forward and the historical backlog
    # was merged (merge_duplicate_claims.py), so duplicate hypothesis groups
    # among non-MERGED claims must stay 0 forever. >0 = the identity chokepoint
    # regressed or a writer is bypassing it; any future merge is a deliberate
    # ledgered one-shot, never automatic (report-only by design).
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from claim_lifecycle import normalize_hypothesis
        import hashlib
        seen, dup_groups = {}, 0
        for cid, hyp in p.execute(
                "SELECT id, hypothesis_text FROM knowledge_claims "
                "WHERE COALESCE(claim_status,'') != 'MERGED'"):
            h = hashlib.sha256(normalize_hypothesis(hyp or "").encode()).hexdigest()[:16]
            if h in seen:
                dup_groups += 1 if seen[h] == 1 else 0
                seen[h] += 1
            else:
                seen[h] = 1
        info["fragment_dup_groups"] = dup_groups
        if dup_groups:
            alerts.append(f"fragment_dup_groups: {dup_groups} duplicate hypothesis groups "
                          f"among non-MERGED claims (identity chokepoint regressed?)")
    except Exception as e:
        info["fragment_dup_groups"] = f"check_error: {e}"

    p.close(); k.close()

    REPORT.write_text(json.dumps({
        "checked_at": now,
        "healthy": not alerts,
        "alerts": alerts,
        "info": info,
    }, indent=1, default=str))

    if alerts:
        for a in alerts:
            print(f"ALERT {a}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
