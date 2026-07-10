"""Characterization (golden-master) tests for apply_worker_results.apply_results().

These tests pin the CURRENT observable behavior of the intake pipeline so it
can be refactored safely: they build a synthetic HERMES_HOME (fresh
prometheus.db from schema/prometheus.schema.sql, a minimal kanban.db, a
self_state.json), insert worker_results rows, call apply_results() directly,
and assert the exact resulting rows/state. If a refactor changes any pinned
value, a test here MUST fail. Do not "fix" a failure by re-pinning unless the
behavior change is intentional.

What is covered (the main happy paths of the ~1,300-line loop):
  - verdict derivation from finding text (CONFIRMED/REFUTED prefix) and the
    text-over-flag override, verdict tag injection, no-double-prefix rule
  - experiments upsert (incl. model default, created_at==completed_at from the
    worker_results timestamp, quality score/tags, confidence_change sign)
  - hypothesis recovery from the kanban task body (HYPOTHESIS: line)
  - experiment_type passthrough vs. auto-classification fallback
  - claim attachment (knowledge_claims + claim_evidence + posterior/status)
  - refutation_type classification
  - parent-curiosity resolution via CURIOSITY_ID and root-curiosity creation
    (Method 4) when no parent exists
  - queue_additions parsing (semicolon form), the weighted-support evidence
    gate ([QA-GATE] blocks plain follow-ups at wsc 0.0, [TRANSFER] exempt),
    transfer_tracking insert for [TRANSFER ...] queue items
  - refutation-branching and confirmed-branching synthetic follow-ups
    (incl. the 80-char hypothesis truncation in generated questions)
  - self_state.json counter fan-out; the low-quality REJECT path (result
    swallowed, counters still bumped); the empty-state path (whole curiosity
    machinery silently skipped); idempotency (applied=1 rows are not re-read)

Also covered (added 2026-07-09 ahead of the REFACTORING.md §2 extraction, so
every stage being moved is pinned):
  - flag-derived verdicts + the verdict-prefix injection branch; PARTIALLY
    REFUTED and REFUTED_SETUP verdict classes and their dedicated
    refutation-branching templates; ISO-string created_at conversion;
    JSON-array tags/queue forms; single-'[' non-JSON queue items and the
    shadow-parser RECOVERY print
  - the caveat confidence cap (intake-side clamp + worker_results write-back)
  - 1e adversarial ATTACK_OUTCOME routing (BROKEN -> DISPUTED, NARROWED ->
    boundary-mapping curiosity, SURVIVED_WITHIN_SCOPE -> claim_scopes append)
  - 1f dispute-arbitration seam (resolve_arbitration stubbed with a recorder;
    call args + stdout pinned — arbitration INTERNALS stay out of scope)
  - 1g retest credit (replication_results insert, source-dedup guard,
    curiosity resolution) and the boundary-lane closure in section 2
    (narrowed_boundary resolve + claim_scopes regime capture)
  - benchmark lineage (evidence-gate bypass, benchmark provenance, CF
    suppression) INCLUDING the dead parent_benchmark_id propagation: 1a-bis
    and experiments.benchmark_id must STAY None because the parent fetch
    happens after the insert — a refactor must not accidentally "fix" this
  - generation-throttle-active branches (should_throttle stubbed True) and
    the deep-lineage (depth >= 8) gate+throttle exemptions
  - [QA-SKIP] too-few-words and [QA-DEDUP] duplicate queue items
  - lineage_live cross-domain detection + update_completed(conn=) completion
  - junk-domain auto-classification (classify_embedding stubbed via
    sys.modules; worker_results.domain deliberately NOT synced for
    classifier-only changes)

Known coverage gaps (paths that need heavier scaffolding or live services):
  - the authority-basis confidence cap (currently dead code in this repo:
    write_worker_result has no parse_basis, so the import always fails)
  - real generation-throttle quota arithmetic (should_throttle is stubbed)
  - real embedding-server classification scores (classify_embedding stubbed)
  - dispute-arbitration resolution internals (resolve_arbitration stubbed)
  - dedup-set overflow (>10k rows); state_write_lock timeout (yields None);
    drift detector internals
  - the __main__ / cron entrypoint

Isolation seams (documented, deliberate):
  - module path globals are repointed at the per-test home
    (state_lock.STATE_PATH is hardcoded to ~/.hermes and MUST be patched so
    tests never touch a live deployment; prometheus_db.DB_PATH/DB_LOCK and
    apply_worker_results.HERMES_HOME are patched for the same reason)
  - drift_detector.run_drift_check is stubbed: the real one reads/writes
    ~/.hermes/inspector/drift_state.json (expanduser, not HERMES_HOME).
    The test still pins THAT apply_results calls it exactly once per
    non-empty batch.
  - domain_creation_gate caches are reset per test and its _DB_PATH is
    pointed at the fixture DB (fresh DB => no canonical domains => every
    fixture domain resolves as "provisional" and passes through unchanged).
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

import apply_worker_results as awr
import domain_creation_gate
import drift_detector
import prometheus_db
import state_lock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "prometheus.schema.sql"

# Fixed worker_results.created_at so experiments timestamps are fully pinned.
WR_CREATED_AT = 1751900000

# ── Scenario data (main golden-master run) ──────────────────────────────────
HYP_A = ("Annealed copper interconnects reduce electromigration failures "
         "in packaging")  # < 80 chars: no truncation in generated follow-ups
FINDING_A = (
    "CONFIRMED: Annealed interconnect batches showed a 31% lower "
    "electromigration failure rate (12.4% -> 8.6% at 2000h, n=40 lots) "
    "because larger grains reduce boundary diffusion pathways; accuracy of "
    "the lifetime model was 0.91."
)
PARENT_TEXT_A = ("Investigate thermal budget interactions for stacked die "
                 "reliability qualification")
QUEUE_A = (
    "Quantify grain boundary diffusion under pulsed current stress conditions; "
    "[TRANSFER from materials_science] Does annealing driven grain growth "
    "extend voltage regulator lifetime?"
)
TASK_A = "task-char-a"
EXP_A = "exp_char_a"
DOMAIN_A = "semiconductor_reliability"

HYP_B = ("Increasing cache prefetch aggressiveness lowers tail latency for "
         "graph analytics workloads")  # 90 chars: pins the [:80] truncation
FINDING_B = (
    "REFUTED: Aggressive prefetch increased p99 latency by 14% "
    "(212ms -> 242ms) across all replay traces because prefetch traffic "
    "evicted hot vertices from the shared L2 cache."
)
TASK_B = "task-char-b"
EXP_B = "exp_char_b"
DOMAIN_B = "systems_performance"


# ── Fixture plumbing ────────────────────────────────────────────────────────

def _build_home(home):
    """Create a synthetic HERMES_HOME: schema-true prometheus.db, minimal
    kanban.db (just the tasks table apply_worker_results reads), and a truthy
    self_state.json (an empty state dict disables the whole curiosity path —
    that behavior is pinned separately in test_empty_state...)."""
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / "prometheus.db")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    conn.close()
    kconn = sqlite3.connect(home / "kanban.db")
    kconn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, "
        "status TEXT)")
    kconn.commit()
    kconn.close()
    (home / "self_state.json").write_text(json.dumps({"agent_id": "char-test"}))


def _patch_paths(home, setattr_fn):
    """Repoint every module-level path global at the per-test home.

    setattr_fn is monkeypatch.setattr under pytest (auto-restore) or the
    builtin setattr in the one-shot observation harness."""
    home_s = str(home)
    db = os.path.join(home_s, "prometheus.db")
    setattr_fn(awr, "HERMES_HOME", home_s)
    setattr_fn(awr, "DB_PATH", db)
    setattr_fn(awr, "STATE_PATH", os.path.join(home_s, "self_state.json"))
    setattr_fn(prometheus_db, "DB_PATH", db)
    setattr_fn(prometheus_db, "DB_LOCK", db + ".lock")
    setattr_fn(state_lock, "STATE_PATH", os.path.join(home_s, "self_state.json"))
    setattr_fn(state_lock, "LOCK_PATH",
               os.path.join(home_s, "self_state.json.lock"))
    setattr_fn(domain_creation_gate, "_DB_PATH", db)
    setattr_fn(domain_creation_gate, "CanonicalCache", None)
    setattr_fn(domain_creation_gate, "_KNOWN_VOCAB_CACHE", None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    _build_home(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    _patch_paths(home, monkeypatch.setattr)
    drift_calls = []
    monkeypatch.setattr(drift_detector, "run_drift_check",
                        lambda *a, **k: drift_calls.append(a) or 0)
    yield {"home": home, "db": str(home / "prometheus.db"),
           "drift_calls": drift_calls}


def _seed_task(home, task_id, title, body):
    kconn = sqlite3.connect(home / "kanban.db")
    kconn.execute(
        "INSERT INTO tasks (id, title, body, status) VALUES (?, ?, ?, 'done')",
        (task_id, title, body))
    kconn.commit()
    kconn.close()


def _seed_curiosity(db, text, **cols):
    """Insert a curiosity; defaults are byte-identical to the original helper
    (text, priority 5, active, created_at WR_CREATED_AT-3600). kwargs add or
    override columns (provenance, source_experiment, benchmark_id, ...)."""
    base = {"text": text, "priority": 5, "status": "active",
            "created_at": float(WR_CREATED_AT - 3600)}
    base.update(cols)
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO curiosities ({}) VALUES ({})".format(
            ", ".join(base), ", ".join("?" for _ in base)),
        tuple(base.values()))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def _seed_experiment(db, exp_id, domain, hypothesis="", result="",
                     status="completed"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO experiments (id, hypothesis, result, status, domain, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (exp_id, hypothesis, result, status, domain,
         float(WR_CREATED_AT - 7200)))
    conn.commit()
    conn.close()


def _seed_claim(db, hypothesis_text, **cols):
    base = {"claim_hash": "seed-hash-" + hypothesis_text[:48],
            "hypothesis_text": hypothesis_text}
    base.update(cols)
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO knowledge_claims ({}) VALUES ({})".format(
            ", ".join(base), ", ".join("?" for _ in base)),
        tuple(base.values()))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def _seed_adv_replication(db, claim_id):
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO adversarial_replications (claim_id, created_at, status) "
        "VALUES (?, ?, 'pending')", (claim_id, float(WR_CREATED_AT - 3600)))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def _seed_worker_result(db, **kw):
    cols = {
        "experiment_id": None, "kanban_task_id": None,
        "hypothesis_supported": None, "key_finding": None, "confidence": None,
        "domain": None, "tags": None, "files_produced": None,
        "queue_additions": None, "worker_id": None, "created_at": WR_CREATED_AT,
        "predicted_direction": None, "observed_direction": None,
        "design_vector": None, "experiment_type": None, "mechanism_type": None,
        "model": None, "calibrated_confidence": None, "verdict_basis": None,
        "applied": 0,
    }
    cols.update(kw)
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO worker_results ({}) VALUES ({})".format(
            ", ".join(cols), ", ".join("?" for _ in cols)),
        tuple(cols.values()))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def _seed_main_scenario(env):
    """Two results: one SUPPORTED (with kanban parent curiosity + queue
    additions), one REFUTED (no CURIOSITY_ID => root-curiosity path)."""
    home, db = env["home"], env["db"]
    parent_id = _seed_curiosity(db, PARENT_TEXT_A)
    _seed_task(home, TASK_A, f"{EXP_A}: anneal study",
               f"INVESTIGATION TASK: {EXP_A}\nHYPOTHESIS: {HYP_A}\n"
               f"CURIOSITY_ID: {parent_id}")
    _seed_task(home, TASK_B, f"{EXP_B}: prefetch study",
               f"HYPOTHESIS: {HYP_B}")
    wr_a = _seed_worker_result(
        db, experiment_id=EXP_A, kanban_task_id=TASK_A,
        hypothesis_supported=1, key_finding=FINDING_A, confidence=0.9,
        domain=DOMAIN_A, tags="CONFIRMED,generalization", files_produced="[]",
        queue_additions=QUEUE_A, worker_id="worker-char-1",
        predicted_direction="increase", observed_direction="increase",
        design_vector='{"n": 40}', experiment_type="EMPIRICAL",
        mechanism_type="MONOTONIC", model="agents-a1",
        verdict_basis="direct_run")
    wr_b = _seed_worker_result(
        db, experiment_id=EXP_B, kanban_task_id=TASK_B,
        hypothesis_supported=0, key_finding=FINDING_B, confidence=0.85,
        domain=DOMAIN_B, tags="REFUTED", worker_id="worker-char-2")
    return {"parent_id": parent_id, "wr_a": wr_a, "wr_b": wr_b}


def _rows(db, sql, params=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return out


def _pop_volatile(row, keys):
    """Remove wall-clock fields; assert they were actually set (> 0)."""
    for k in keys:
        v = row.pop(k)
        assert v is not None and float(v) > 0, f"{k} not set: {v!r}"
    return row


# ── Tests ───────────────────────────────────────────────────────────────────

def test_main_scenario_golden_master(env, capsys):
    ids = _seed_main_scenario(env)
    db = env["db"]

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 2
    assert env["drift_calls"] == [()]  # advisory drift check: once per batch

    # ── worker_results marked applied ──
    wrs = _rows(db, "SELECT id, applied FROM worker_results ORDER BY id")
    assert wrs == [{"id": ids["wr_a"], "applied": 1},
                   {"id": ids["wr_b"], "applied": 1}]

    # ── experiments (full-row pins) ──
    exps = _rows(db, "SELECT * FROM experiments ORDER BY id")
    assert [e["id"] for e in exps] == [EXP_A, EXP_B]
    exp_a, exp_b = exps
    assert exp_a == {
        "id": EXP_A,
        "cycle_id": None,
        "hypothesis": HYP_A,
        "result": FINDING_A,          # already verdict-prefixed: no re-prefix
        "status": "completed",
        "confidence_change": 0.9,     # SUPPORTED => +confidence
        "tags": '["SUPPORTED", "CONFIRMED", "generalization"]',
        "domain": DOMAIN_A,
        "model": "agents-a1",
        "created_at": float(WR_CREATED_AT),
        "started_at": None,
        "completed_at": float(WR_CREATED_AT),
        "workspace_path": None,
        "kanban_task_id": TASK_A,
        "refutation_type": "SUPPORTED",
        "predicted_direction": "increase",
        "observed_direction": "increase",
        "design_vector": '{"n": 40}',
        "quality_score": 100,
        "quality_tags": '["NO_MECHANISM", "QUALITY_HIGH"]',
        "metrics_json": None,
        "experiment_type": "EMPIRICAL",   # passthrough when worker set it
        "mechanism_type": "MONOTONIC",
        "benchmark_id": None,
        "verdict_basis": "direct_run",
    }
    assert exp_b == {
        "id": EXP_B,
        "cycle_id": None,
        "hypothesis": HYP_B,
        "result": FINDING_B,
        "status": "completed",
        "confidence_change": -0.85,   # REFUTED => -confidence
        "tags": '["REFUTED"]',
        "domain": DOMAIN_B,
        "model": "xiaomi/mimo-v2.5",  # default when worker_results.model NULL
        "created_at": float(WR_CREATED_AT),
        "started_at": None,
        "completed_at": float(WR_CREATED_AT),
        "workspace_path": None,
        "kanban_task_id": TASK_B,
        # NOTE: a plain REFUTED verdict without directional metadata lands as
        # UNCERTAIN — classify_refutations needs more signal to subtype it.
        "refutation_type": "UNCERTAIN",
        "predicted_direction": None,
        "observed_direction": None,
        "design_vector": None,
        "quality_score": 100,
        "quality_tags": '["NO_MECHANISM", "QUALITY_HIGH"]',
        "metrics_json": None,
        "experiment_type": "MECHANISTIC",  # auto-classified fallback default
        "mechanism_type": None,
        "benchmark_id": None,
        "verdict_basis": None,
    }

    # ── knowledge_claims + claim_evidence ──
    claims = _rows(db, "SELECT * FROM knowledge_claims ORDER BY id")
    assert len(claims) == 2
    claim_a = _pop_volatile(claims[0], ["created_at", "last_updated_at"])
    claim_b = _pop_volatile(claims[1], ["created_at", "last_updated_at"])
    assert claim_a["hypothesis_text"] == HYP_A
    assert claim_a["domain"] == DOMAIN_A
    assert claim_a["support_count"] == 1
    assert claim_a["refute_count"] == 0
    assert claim_a["total_evidence"] == 1
    assert claim_a["posterior"] == pytest.approx(2.0 / 3.0)
    assert claim_a["status"] == "UNTESTED"      # 1 test < MIN_TESTS_FOR_ACTIVE
    assert claim_a["claim_status"] == "HISTORICAL_UNKNOWN"  # schema default
    assert claim_a["first_experiment_id"] == EXP_A
    assert claim_a["last_experiment_id"] == EXP_A
    assert claim_a["claim_type"] == "DIRECTIONAL"
    assert claim_a["is_meta"] == 0
    assert claim_a["is_empirical_fact"] == 0
    # refresh_claim_summary is a silent no-op on this path (best-effort):
    assert claim_a["claim_summary"] is None
    assert claim_b["hypothesis_text"] == HYP_B
    assert claim_b["support_count"] == 0
    assert claim_b["refute_count"] == 1
    assert claim_b["posterior"] == pytest.approx(1.0 / 3.0)
    assert claim_b["status"] == "UNTESTED"
    assert claim_b["claim_summary"] is None

    evidence = _rows(
        db, "SELECT claim_id, experiment_id, worker_result_id, evidence_type,"
            " confidence, domain FROM claim_evidence ORDER BY id")
    assert evidence == [
        {"claim_id": claims[0]["id"], "experiment_id": EXP_A,
         "worker_result_id": ids["wr_a"], "evidence_type": "support",
         "confidence": 0.9, "domain": DOMAIN_A},
        {"claim_id": claims[1]["id"], "experiment_id": EXP_B,
         "worker_result_id": ids["wr_b"], "evidence_type": "refute",
         "confidence": 0.85, "domain": DOMAIN_B},
    ]

    # ── curiosities: parent resolution, gate block, branching inserts ──
    curs = _rows(
        db, "SELECT id, text, priority, status, source_experiment,"
            " resolved_by_experiment, parent_curiosity_id, provenance"
            " FROM curiosities ORDER BY id")

    hyp_b80 = HYP_B[:80]
    expected_texts = [
        PARENT_TEXT_A,
        "[TRANSFER from materials_science] Does annealing driven grain "
        "growth extend voltage regulator lifetime?",
        f"[TRANSFER] Does the mechanism confirmed in '{HYP_A}' transfer to "
        f"a related domain?",
        f"What boundary conditions would cause '{HYP_A}' to fail?",
        HYP_B,
        f"Which specific variable, if changed, would flip the result of "
        f"'{hyp_b80}' from refuted to supported?",
        f"[TRANSFER] Does the mechanism tested in '{hyp_b80}' behave "
        f"differently in a neighboring domain?",
    ]
    assert [c["text"] for c in curs] == expected_texts

    parent, transfer_q, cf1, cf2, root_b, rf1, rf2 = curs
    # Parent question resolved by the experiment it spawned:
    assert parent["status"] == "resolved"
    assert parent["source_experiment"] == EXP_A
    assert parent["resolved_by_experiment"] == EXP_A
    # Worker queue [TRANSFER] item survives the evidence gate; the plain
    # follow-up in QUEUE_A is gate-blocked (wsc 0.0 < 1.0) and never lands:
    assert transfer_q["status"] == "active"
    assert transfer_q["priority"] == 3
    assert transfer_q["source_experiment"] == EXP_A
    assert transfer_q["parent_curiosity_id"] == ids["parent_id"]
    assert json.loads(transfer_q["provenance"]) == {
        "parser": "new", "origin": "transfer", "interventions": ["I6"],
        "parent_source": EXP_A, "created_at_fix": True}
    for c in (cf1, cf2):
        assert json.loads(c["provenance"]) == {
            "parser": "new", "origin": "confirmed_followup",
            "interventions": ["I6", "confirmed_branching"],
            "parent_source": EXP_A, "verdict": "SUPPORTED",
            "created_at_fix": True}
        assert c["parent_curiosity_id"] == ids["parent_id"]
    # Method-4 root curiosity for the parentless refuted result:
    assert root_b["status"] == "active"       # never resolved (has source_exp)
    assert root_b["source_experiment"] == EXP_B
    assert root_b["resolved_by_experiment"] is None
    assert root_b["parent_curiosity_id"] is None
    assert root_b["provenance"] is None
    for c in (rf1, rf2):
        assert json.loads(c["provenance"]) == {
            "parser": "new", "origin": "refutation_followup",
            "interventions": ["I6", "refutation_branching"],
            "parent_source": None, "verdict": "REFUTED",
            "created_at_fix": True}
        assert c["parent_curiosity_id"] == root_b["id"]

    # ── transfer_tracking: only the queue [TRANSFER ...] item creates a row
    # (generated [TRANSFER] follow-ups do NOT) ──
    transfers = _rows(
        db, "SELECT source_result_id, source_domain, target_domain, status,"
            " transfer_source, task_id FROM transfer_tracking")
    assert transfers == [{
        "source_result_id": ids["wr_a"],
        "source_domain": DOMAIN_A,
        # quirk preserved: '[TRANSFER from X]' parses X as the TARGET domain
        "target_domain": "materials_science",
        "status": "queued",
        "transfer_source": "explicit_transfer",
        "task_id": None,
    }]

    # ── artifact manifest frozen at intake (step 1d) ──
    manifest = json.loads(
        (env["home"] / "artifacts" / TASK_A / "manifest.json").read_text())
    manifest.pop("intake_finalized_at")
    assert manifest == {
        "verdict": "SUPPORTED",
        "verdict_basis": "direct_run",
        "confidence": 0.9,
        "quality_score": 100,
        "quality_tags": ["NO_MECHANISM", "QUALITY_HIGH"],
        "domain": DOMAIN_A,
        "hypothesis": HYP_A,
        "key_finding": FINDING_A,
        "worker_id": "worker-char-1",
        "model": "agents-a1",
        "experiment_id": EXP_A,
        "kanban_task_id": TASK_A,
    }
    manifest_b = json.loads(
        (env["home"] / "artifacts" / TASK_B / "manifest.json").read_text())
    assert manifest_b["verdict"] == "REFUTED"
    assert manifest_b["model"] is None   # raw worker value, not the default

    # ── self_state.json counter fan-out ──
    state = json.loads((env["home"] / "self_state.json").read_text())
    last_updated = state.pop("last_updated")
    assert last_updated > 0
    assert state == {
        "agent_id": "char-test",
        "experiments_completed_list": [EXP_A, EXP_B],
        "experiments_completed_list_len": 2,
        "completed": 2,
        "experiments_completed": 2,
        "total_experiments_completed": 2,
        "total_experiments": 2,
        "experiments_completed_count": 2,
        "count": 2,
        "completed_count": 2,
        "experiments": 2,
        "metrics": {"experiments_completed": 2, "experiments_conducted": 2,
                    "experiments_completed_count": 2},
        "counters": {"experiments_completed": 2, "experiments_total": 2,
                     "total_experiments": 2, "experiments_completed_count": 2,
                     "count": 2},
    }

    # ── stdout routing markers ──
    assert "[QA-GATE] evidence gate blocked" in out
    assert ("[QA-INSERT] curiosity created: '[TRANSFER from materials_science]"
            in out)
    assert out.count("[CF-INSERT]") == 2
    assert out.count("[RF-INSERT]") == 2
    assert f"Applied: {EXP_A}" in out
    assert f"Applied: {EXP_B}" in out
    assert "Applied 2 worker results" in out

    # ── idempotency: applied rows are never re-read ──
    assert awr.apply_results() == 0
    assert len(_rows(db, "SELECT id FROM curiosities")) == 7


def test_low_quality_result_rejected_but_counted(env, capsys):
    db = env["db"]
    wr = _seed_worker_result(
        db, experiment_id="exp_char_rej", key_finding="no data",
        confidence=0.7, domain="testing", worker_id="worker-char-3")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 1
    assert "REJECTED" in out
    # Zombie prevention: nothing written downstream...
    assert _rows(db, "SELECT * FROM experiments") == []
    assert _rows(db, "SELECT * FROM knowledge_claims") == []
    assert _rows(db, "SELECT * FROM curiosities") == []
    # ...but the row is consumed and the state counters still move:
    assert _rows(db, "SELECT applied FROM worker_results WHERE id = ?",
                 (wr,)) == [{"applied": 1}]
    state = json.loads((env["home"] / "self_state.json").read_text())
    assert state == {
        "agent_id": "char-test",
        "experiments_completed_list": ["exp_char_rej"],
        "metrics": {"experiments_run": 1, "hypotheses_tested": 1},
    }
    # Asymmetry pin: the rejected path bumps ONLY these keys — none of the
    # completed-count fan-out fields are written.
    assert "experiments_completed_list_len" not in state


def test_empty_state_skips_curiosity_machinery(env, capsys):
    """With a falsy self_state.json ({}), the entire section-2 block —
    curiosity creation, branching, counters — is silently skipped, while the
    DB-side writes (experiments, claims) still happen."""
    db = env["db"]
    (env["home"] / "self_state.json").write_text("{}")
    _seed_task(env["home"], TASK_A, f"{EXP_A}: anneal study",
               f"HYPOTHESIS: {HYP_A}")
    _seed_worker_result(
        db, experiment_id=EXP_A, kanban_task_id=TASK_A,
        hypothesis_supported=1, key_finding=FINDING_A, confidence=0.9,
        domain=DOMAIN_A, tags="CONFIRMED", queue_additions=QUEUE_A,
        worker_id="worker-char-1")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 1
    # Misleading-but-current WARN even though the file exists (it is just {}):
    assert "WARN: self_state.json not found, skipping state update" in out
    assert len(_rows(db, "SELECT id FROM experiments")) == 1
    assert len(_rows(db, "SELECT id FROM knowledge_claims")) == 1
    assert _rows(db, "SELECT * FROM curiosities") == []       # all skipped
    assert _rows(db, "SELECT * FROM transfer_tracking") == []
    assert json.loads((env["home"] / "self_state.json").read_text()) == {}


def test_text_verdict_overrides_supported_flag(env, capsys):
    """Workers sometimes pass --supported while writing REFUTED in the text;
    the finding text is the source of truth for the verdict AND for the
    claim evidence direction."""
    db = env["db"]
    _seed_task(env["home"], TASK_B, f"{EXP_B}: prefetch study",
               f"HYPOTHESIS: {HYP_B}")
    wr = _seed_worker_result(
        db, experiment_id=EXP_B, kanban_task_id=TASK_B,
        hypothesis_supported=1,          # flag says supported...
        key_finding=FINDING_B,           # ...text says REFUTED: text wins
        confidence=0.7, domain=DOMAIN_B, tags="", worker_id="worker-char-2")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 1
    exp = _rows(db, "SELECT result, tags, confidence_change,"
                    " refutation_type FROM experiments WHERE id = ?",
                (EXP_B,))[0]
    assert exp["result"] == FINDING_B
    assert exp["tags"] == '["REFUTED"]'
    assert exp["confidence_change"] == -0.7
    assert exp["refutation_type"] == "UNCERTAIN"  # see main-scenario note
    claim = _rows(db, "SELECT support_count, refute_count, posterior"
                      " FROM knowledge_claims")[0]
    assert claim == {"support_count": 0, "refute_count": 1,
                     "posterior": pytest.approx(1.0 / 3.0)}
    evid = _rows(db, "SELECT evidence_type, worker_result_id"
                     " FROM claim_evidence")
    assert evid == [{"evidence_type": "refute", "worker_result_id": wr}]
    assert "TEXT-FLAG MISMATCH" in out
    # Refutation branching still fires off the text-derived verdict:
    assert out.count("[RF-INSERT]") == 2


# ── Extension tests (2026-07-09): pin every stage ahead of the §2 extraction ─

HYP_C = ("Annealing at 300C stabilizes wafer yield under rapid thermal "
         "cycling in production lots")
FINDING_C = ("Annealing at 300C improved yield by 12% (34% -> 46%, n=30 lots) "
             "because grain growth reduced void density at the via interface.")
HYP_D = ("Pinned shared caches remove tail latency regressions for mixed "
         "inference workloads")
FINDING_D = ("Latency regressed 18% (44ms -> 52ms) across replay traces "
             "despite cache pinning, n=12 hosts, because pin churn evicted "
             "the hot working set.")
QUEUE_D_RAW = ("[TRANSFER from cache_systems] Does pinned-cache latency "
               "regression appear in GPU inference serving?")


def test_flag_verdict_prefixing_iso_timestamp_and_parse_variants(env, capsys):
    """Pins (a) the flag-derived verdict fallback AND the verdict-prefix
    injection branch (finding text carries no verdict), (b) ISO-string
    created_at conversion, (c) JSON-array tags and queue forms, (d) the
    single-'[' non-JSON queue item and the shadow-parser RECOVERY print."""
    db = env["db"]
    _seed_task(env["home"], "task-char-c", "exp_char_c: anneal stability",
               f"HYPOTHESIS: {HYP_C}")
    _seed_task(env["home"], "task-char-d", "exp_char_d: cache pinning",
               f"HYPOTHESIS: {HYP_D}")
    _seed_worker_result(
        db, experiment_id="exp_char_c", kanban_task_id="task-char-c",
        hypothesis_supported=1,           # no verdict in text: flag decides
        key_finding=FINDING_C, confidence=0.75, domain=DOMAIN_A,
        tags='["alpha", "beta"]',          # JSON-array tag form
        queue_additions='["Does the yield gain persist at 350C under thermal '
                        'cycling stress?", "Quantify void density change '
                        'across anneal temperatures and ramp rates"]',
        worker_id="worker-char-4",
        created_at="2026-07-07T12:00:00Z")  # ISO form
    wr_d = _seed_worker_result(
        db, experiment_id="exp_char_d", kanban_task_id="task-char-d",
        hypothesis_supported=0,           # flag says refuted, text is plain
        key_finding=FINDING_D, confidence=0.7, domain=DOMAIN_B,
        queue_additions=QUEUE_D_RAW,       # '['-but-not-JSON single item
        worker_id="worker-char-5")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 2
    exps = _rows(db, "SELECT id, result, tags, confidence_change, created_at,"
                     " completed_at FROM experiments ORDER BY id")
    assert exps == [
        {"id": "exp_char_c",
         "result": f"SUPPORTED: {FINDING_C}",   # prefix injected
         "tags": '["SUPPORTED", "alpha", "beta"]',
         "confidence_change": 0.75,
         "created_at": 1783425600.0,            # 2026-07-07T12:00:00Z
         "completed_at": 1783425600.0},
        {"id": "exp_char_d",
         "result": f"REFUTED: {FINDING_D}",     # prefix injected
         "tags": '["REFUTED"]',
         "confidence_change": -0.7,
         "created_at": float(WR_CREATED_AT),
         "completed_at": float(WR_CREATED_AT)},
    ]

    # JSON queue parsed into 2 items; both gate-blocked (wsc 0.0, no TRANSFER)
    assert out.count("[QA-GATE] evidence gate blocked") == 2
    assert "'Does the yield gain persist at 350C" in out
    # Shadow parser: JSON-array queue -> old parser agrees (PLR 0)...
    assert "[SHADOW-PARSER] INPUT=2 RECOVERY=0 PLR=0/2=0.00%" in out
    # ...single-'[' non-JSON queue -> old parser dropped it (the key metric)
    assert "[SHADOW-PARSER] INPUT=1 RECOVERY=1 PLR=1/1=100.00%" in out
    assert f"    [RECOVERED] '{QUEUE_D_RAW[:80]}'" in out

    # The recovered [TRANSFER item lands + tracks a transfer
    transfer = _rows(db, "SELECT source_domain, target_domain, status,"
                         " transfer_source FROM transfer_tracking")
    assert transfer == [{"source_domain": DOMAIN_B,
                         "target_domain": "cache_systems",
                         "status": "queued",
                         "transfer_source": "explicit_transfer"}]
    assert out.count("[QA-INSERT]") == 1
    # Flag-refuted result still branches (2 RF) and flag-supported one (2 CF)
    assert out.count("[RF-INSERT]") == 2
    assert out.count("[CF-INSERT]") == 2


def test_partially_refuted_and_refuted_setup_branching(env, capsys):
    """PARTIALLY REFUTED and REFUTED_SETUP verdict classes: tag mapping,
    negative confidence_change, and their dedicated refutation-branching
    templates (partial-support mutation pair vs. the single simplified-design
    follow-up)."""
    db = env["db"]
    hyp_p = ("Prefetch reordering lowers both median and tail latency for "
             "columnar scan workloads")
    finding_p = ("PARTIALLY REFUTED: Reordering helped p50 (-9%, 120ms -> "
                 "109ms) but hurt p99 (+6%, n=18 traces) because prefetch "
                 "bursts collided with compaction IO.")
    hyp_s = ("Speculative page pinning reduces cold-start latency in "
             "serverless snapshot restores")
    finding_s = ("REFUTED_SETUP: Harness lost the snapshot volume before "
                 "load generation started (n=0 restores measured), so the "
                 "pinning pathway was never exercised.")
    _seed_task(env["home"], "task-char-p", "exp_char_p: prefetch reorder",
               f"HYPOTHESIS: {hyp_p}")
    _seed_task(env["home"], "task-char-s", "exp_char_s: page pinning",
               f"HYPOTHESIS: {hyp_s}")
    _seed_worker_result(
        db, experiment_id="exp_char_p", kanban_task_id="task-char-p",
        hypothesis_supported=0, key_finding=finding_p, confidence=0.8,
        domain=DOMAIN_B, worker_id="worker-char-6")
    _seed_worker_result(
        db, experiment_id="exp_char_s", kanban_task_id="task-char-s",
        hypothesis_supported=0, key_finding=finding_s, confidence=0.55,
        domain=DOMAIN_B, worker_id="worker-char-7")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 2
    exps = _rows(db, "SELECT id, result, tags, confidence_change,"
                     " refutation_type FROM experiments ORDER BY id")
    assert exps == [
        {"id": "exp_char_p", "result": finding_p,
         "tags": '["PARTIAL"]',                # PARTIALLY REFUTED -> PARTIAL
         "confidence_change": -0.8,
         "refutation_type": "UNCERTAIN"},
        {"id": "exp_char_s", "result": finding_s,
         "tags": '["REFUTED"]',                # REFUTED_SETUP -> REFUTED tag
         "confidence_change": -0.55,
         "refutation_type": "UNCERTAIN"},
    ]

    hyp_p80, hyp_s80 = hyp_p[:80], hyp_s[:80]
    rf_texts = [c["text"] for c in _rows(
        db, "SELECT text FROM curiosities WHERE provenance LIKE"
            " '%refutation_followup%' ORDER BY id")]
    assert rf_texts == [
        f"Which specific parameter in '{hyp_p80}' showed partial support, "
        f"and does strengthening it push toward full support?",
        f"[TRANSFER] Does the partially-supported component of '{hyp_p80}' "
        f"transfer to a related domain?",
        f"Can '{hyp_s80}' be tested with a simplified experimental design "
        f"using fewer variables?",
    ]
    assert out.count("[RF-INSERT]") == 3
    assert out.count("[CF-INSERT]") == 0

    # Both verdicts count as refute evidence on their claims
    claims = _rows(db, "SELECT hypothesis_text, support_count, refute_count"
                       " FROM knowledge_claims ORDER BY id")
    assert claims == [
        {"hypothesis_text": hyp_p, "support_count": 0, "refute_count": 1},
        {"hypothesis_text": hyp_s, "support_count": 0, "refute_count": 1},
    ]


def test_caveat_confidence_cap(env, capsys):
    """The blocker-caveat clamp: a finding admitting no independent
    verification caps confidence at 0.6 BEFORE claim evidence and
    confidence_change, tags the experiment, and writes the cap back onto the
    worker_results row (confidence, calibrated_confidence, tags)."""
    db = env["db"]
    hyp = ("Published lattice coupling constants reproduce the reported "
           "phase boundary in simulation")
    finding = ("CONFIRMED: Simulated lattice runs match the published "
               "coupling constants (delta 0.02, n=25 runs) because the model "
               "tracks the reported values, but without independent "
               "verification of the original dataset.")
    _seed_task(env["home"], "task-char-cav", "exp_char_cav: lattice repro",
               f"HYPOTHESIS: {hyp}")
    wr = _seed_worker_result(
        db, experiment_id="exp_char_cav", kanban_task_id="task-char-cav",
        hypothesis_supported=1, key_finding=finding, confidence=0.9,
        domain=DOMAIN_A, worker_id="worker-char-8")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 1
    assert "CAVEAT CAP: exp_char_cav confidence 0.90 -> 0.60" in out
    exp = _rows(db, "SELECT tags, confidence_change FROM experiments")[0]
    assert exp == {"tags": '["SUPPORTED", "CAVEAT_CONF_CAPPED"]',
                   "confidence_change": 0.6}
    wr_row = _rows(db, "SELECT confidence, calibrated_confidence, tags"
                       " FROM worker_results WHERE id = ?", (wr,))[0]
    assert wr_row == {"confidence": 0.6, "calibrated_confidence": 0.6,
                      "tags": "CAVEAT_CONF_CAPPED"}
    evid = _rows(db, "SELECT evidence_type, confidence FROM claim_evidence")
    assert evid == [{"evidence_type": "support", "confidence": 0.6}]
    manifest = json.loads(
        (env["home"] / "artifacts" / "task-char-cav" / "manifest.json")
        .read_text())
    assert manifest["confidence"] == 0.6


def test_adversarial_attack_outcome_routing(env, capsys):
    """1e: explicit ATTACK_OUTCOME tokens route pending adversarial
    replications — BROKEN disputes the claim, NARROWED spawns the
    boundary-mapping curiosity, SURVIVED_WITHIN_SCOPE records survival AND
    appends the incidental limit to claim_scopes (no boundary question)."""
    db = env["db"]
    cid_broken = _seed_claim(db, "Removing the covariate control preserves "
                                 "the anomaly effect size")
    cid_narrow = _seed_claim(db, "Streaming compression doubles throughput "
                                 "for arbitrary payloads")
    cid_scope = _seed_claim(db, "Work-stealing scheduling cuts p95 latency "
                                "for small payloads")
    for cid in (cid_broken, cid_narrow, cid_scope):
        _seed_adv_replication(db, cid)

    f_broken = ("REFUTED: Attack reproduced the anomaly only after removing "
                "the control; the effect does not survive re-analysis "
                "(0.31 -> 0.04, n=60) because the covariate leak drove the "
                "signal. ATTACK_OUTCOME: BROKEN")
    f_narrow = ("PARTIALLY REFUTED: The compression gain holds for text "
                "payloads but fails on binary streams (2.1x -> 1.02x, n=45) "
                "because entropy is already maximal there. "
                "ATTACK_OUTCOME: NARROWED")
    f_scope = ("CONFIRMED: Within the mapped regime (payloads < 64KB) the "
               "scheduling gain persists under attack replication "
               "(14.8% +/- 0.9, n=80) because queue depth stays below "
               "saturation. ATTACK_OUTCOME: SURVIVED_WITHIN_SCOPE. "
               "Incidental limit: the gain vanishes beyond 8 workers.")
    scenarios = [
        ("task-adv-b", "exp_adv_b", cid_broken, f_broken,
         "Attack the covariate anomaly claim with the control restored"),
        ("task-adv-n", "exp_adv_n", cid_narrow, f_narrow,
         "Attack the compression throughput claim on binary payloads"),
        ("task-adv-s", "exp_adv_s", cid_scope, f_scope,
         "Attack the scheduling claim within its mapped payload regime"),
    ]
    for task, exp, cid, finding, hyp in scenarios:
        _seed_task(env["home"], task, f"{exp}: adversarial replication",
                   f"HYPOTHESIS: {hyp}\n"
                   f"ADVERSARIAL_REPLICATION_FOR_CLAIM: {cid}")
        _seed_worker_result(
            db, experiment_id=exp, kanban_task_id=task,
            hypothesis_supported=None, key_finding=finding, confidence=0.8,
            domain=DOMAIN_B, worker_id="worker-adv")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 3
    advs = _rows(db, "SELECT claim_id, status, experiment_id, notes"
                     " FROM adversarial_replications ORDER BY id")
    assert advs == [
        {"claim_id": cid_broken, "status": "refuted",
         "experiment_id": "exp_adv_b", "notes": f_broken[:200]},
        {"claim_id": cid_narrow, "status": "narrowed",
         "experiment_id": "exp_adv_n", "notes": f_narrow[:200]},
        {"claim_id": cid_scope, "status": "survived",
         "experiment_id": "exp_adv_s", "notes": f_scope[:200]},
    ]
    resolved = _rows(db, "SELECT resolved_at FROM adversarial_replications")
    assert all(r["resolved_at"] and r["resolved_at"] > 0 for r in resolved)

    # BROKEN -> claim disputed
    broken = _rows(db, "SELECT claim_status, contradiction_count"
                       " FROM knowledge_claims WHERE id = ?", (cid_broken,))[0]
    assert broken == {"claim_status": "DISPUTED", "contradiction_count": 1}
    assert f"[ADVERSARIAL] claim {cid_broken}: refuted by exp_adv_b" in out
    assert f"[ADVERSARIAL] claim {cid_broken} DISPUTED (attack succeeded)" in out

    # NARROWED -> boundary-mapping curiosity (provenance ledger row)
    bnd = _rows(db, "SELECT text, priority, status, source_experiment,"
                    " source_result_id FROM curiosities"
                    " WHERE provenance = 'narrowed_boundary'")
    assert len(bnd) == 1
    assert bnd[0]["text"].startswith(
        f"[BOUNDARY] Claim {cid_narrow} survived attack only in a narrowed "
        f"regime — map the boundary: state the surviving core, the regime "
        f"where it holds, and where it fails. ATTACK FINDING: ")
    assert bnd[0]["text"].endswith(f_narrow[:400])
    assert bnd[0]["priority"] == 5
    assert bnd[0]["status"] == "active"
    assert bnd[0]["source_experiment"] == "exp_adv_n"
    assert bnd[0]["source_result_id"] is not None
    assert f"[BOUNDARY] spawned boundary-mapping curiosity for claim {cid_narrow}" in out

    # SURVIVED_WITHIN_SCOPE -> survival credit + claim_scopes append, and the
    # refinement loop terminates: NO boundary question for that claim
    scopes = _rows(db, "SELECT claim_id, scope_text, source_experiment,"
                       " source_curiosity_id FROM claim_scopes")
    assert scopes == [{"claim_id": cid_scope, "scope_text": f_scope[:1200],
                       "source_experiment": "exp_adv_s",
                       "source_curiosity_id": None}]
    assert f"[SCOPE] within-scope survival: incidental limit appended for claim {cid_scope}" in out
    assert not any(f"Claim {cid_scope} " in c["text"] for c in bnd)


def test_dispute_arbitration_seam(env, capsys, monkeypatch):
    """1f: a DISPUTE_ARBITRATION_FOR_CLAIM marker + ARBITRATION_VERDICT token
    invoke resolve_arbitration exactly once with the open intake conn.
    Internals are the enqueuer's own concern — the recorder pins the seam."""
    import dispute_arbitration_enqueuer as dae
    db = env["db"]
    calls = []

    def fake_resolve(conn, claim_id, verdict_token, exp_id, finding, now=None):
        calls.append({"claim_id": claim_id, "token": verdict_token,
                      "exp_id": exp_id, "finding": finding, "now": now})
        return "arbitrated_regime_split"

    monkeypatch.setattr(dae, "resolve_arbitration", fake_resolve)

    hyp = ("Cache pinning helps under low churn but hurts under high churn "
           "in shared tenancy")
    finding = ("CONFIRMED: Discriminating run shows both sides were right in "
               "different regimes (churn < 3%/min favors pinning, n=24) "
               "because eviction pressure inverts the benefit. "
               "ARBITRATION_VERDICT: REGIME_SPLIT")
    _seed_task(env["home"], "task-arb-1", "exp_arb_1: arbitration",
               f"HYPOTHESIS: {hyp}\nDISPUTE_ARBITRATION_FOR_CLAIM: 431")
    _seed_worker_result(
        db, experiment_id="exp_arb_1", kanban_task_id="task-arb-1",
        hypothesis_supported=1, key_finding=finding, confidence=0.8,
        domain=DOMAIN_B, worker_id="worker-arb")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 1
    assert len(calls) == 1
    call = calls[0]
    assert call["claim_id"] == 431
    assert call["token"] == "REGIME_SPLIT"
    assert call["exp_id"] == "exp_arb_1"
    assert call["finding"] == finding
    assert call["now"] and call["now"] > 0
    assert ("[ARBITRATION] claim 431: REGIME_SPLIT -> "
            "arbitrated_regime_split by exp_arb_1") in out


def test_retest_credit_and_boundary_closure(env, capsys):
    """1g retest credit + the section-2 boundary-lane closure.

    R1: a [CANDIDATE-RETEST] child records a replication_results row keyed on
    the SOURCE experiment and resolves its curiosity at credit time.
    R2: a second retest of the SAME original is deduped (no second row) but
    its curiosity still resolves — the poisoned-dedup loop fix.
    R3: a narrowed_boundary curiosity is resolved on completion of its own
    task and the mapped regime lands in claim_scopes."""
    db = env["db"]
    _seed_experiment(db, "exp_orig_rt1", "systems_performance",
                     hypothesis="Original scheduling gain hypothesis",
                     result="CONFIRMED: original gain 12% (n=40)")
    cur_rt1 = _seed_curiosity(
        db, "[CANDIDATE-RETEST] Independently re-run exp_orig_rt1 with a "
            "fresh workload trace", provenance="candidate_retest",
        source_experiment="exp_orig_rt1")
    cur_rt2 = _seed_curiosity(
        db, "[CANDIDATE-RETEST] Second independent re-run of exp_orig_rt1 "
            "under production traffic", provenance="candidate_retest",
        source_experiment="exp_orig_rt1")
    cur_bnd = _seed_curiosity(
        db, "[BOUNDARY] Claim 4242 survived attack only in a narrowed regime "
            "— map the boundary: state the surviving core, the regime where "
            "it holds, and where it fails.",
        provenance="narrowed_boundary", source_experiment="exp_attack_x")

    hyp_rt1 = ("Re-running the scheduling gain experiment reproduces the "
               "12% improvement on fresh traces")
    hyp_rt2 = ("A second independent re-run reproduces the scheduling gain "
               "under production traffic")
    hyp_bnd = ("The scheduling gain survives only below 64KB payloads in "
               "the narrowed regime")
    _seed_task(env["home"], "task-rt-1", "exp_rt_1: retest",
               f"HYPOTHESIS: {hyp_rt1}\nCURIOSITY_ID: {cur_rt1}")
    _seed_task(env["home"], "task-rt-2", "exp_rt_2: repeat retest",
               f"HYPOTHESIS: {hyp_rt2}\nCURIOSITY_ID: {cur_rt2}")
    _seed_task(env["home"], "task-bnd-1", "exp_bnd_1: boundary mapping",
               f"HYPOTHESIS: {hyp_bnd}\nCURIOSITY_ID: {cur_bnd}")
    f_rt1 = ("CONFIRMED: Replication reproduced the gain (11.6% vs 12.0%, "
             "n=40 fresh traces) because the queue-depth mechanism held.")
    f_rt2 = ("CONFIRMED: Second replication also reproduced the gain "
             "(12.3%, n=35 production windows) because load mix was stable.")
    f_bnd = ("CONFIRMED: The gain survives only below 64KB payloads; above "
             "that queueing dominates (n=50) because saturation flips the "
             "bottleneck to memory bandwidth.")
    _seed_worker_result(
        db, experiment_id="exp_rt_1", kanban_task_id="task-rt-1",
        hypothesis_supported=1, key_finding=f_rt1, confidence=0.85,
        domain="systems_performance", worker_id="worker-rt-1")
    _seed_worker_result(
        db, experiment_id="exp_rt_2", kanban_task_id="task-rt-2",
        hypothesis_supported=1, key_finding=f_rt2, confidence=0.8,
        domain="systems_performance", worker_id="worker-rt-2")
    _seed_worker_result(
        db, experiment_id="exp_bnd_1", kanban_task_id="task-bnd-1",
        hypothesis_supported=1, key_finding=f_bnd, confidence=0.8,
        domain="systems_performance", worker_id="worker-bnd")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 3
    reps = _rows(db, "SELECT original_experiment_id, original_finding,"
                     " original_domain, validation_task_id,"
                     " validation_experiment_id, validation_finding,"
                     " validation_confidence,"
                     " validation_hypothesis_supported, replication_status,"
                     " selected_at, selection_reason"
                     " FROM replication_results")
    assert reps == [{
        "original_experiment_id": "exp_orig_rt1",
        "original_finding": "CONFIRMED: original gain 12% (n=40)",
        "original_domain": "systems_performance",
        "validation_task_id": "task-rt-1",
        "validation_experiment_id": "exp_rt_1",
        "validation_finding": f_rt1,
        "validation_confidence": 0.85,
        "validation_hypothesis_supported": 1,
        "replication_status": "replicated",
        "selected_at": float(WR_CREATED_AT - 3600),  # curiosity created_at
        "selection_reason": "candidate_retest_intake",
    }]
    assert "[RETEST] exp_rt_1 -> replicated (original exp_orig_rt1)" in out
    # The REPEAT retest earned no second credit row...
    assert "[RETEST] exp_rt_2" not in out
    # ...but BOTH retest curiosities resolve (no re-dispatch loop)
    curs = _rows(db, "SELECT id, status, resolved_by_experiment"
                     " FROM curiosities WHERE id IN (?, ?, ?) ORDER BY id",
                 (cur_rt1, cur_rt2, cur_bnd))
    assert curs == [
        {"id": cur_rt1, "status": "resolved",
         "resolved_by_experiment": "exp_rt_1"},
        {"id": cur_rt2, "status": "resolved",
         "resolved_by_experiment": "exp_rt_2"},
        {"id": cur_bnd, "status": "resolved",
         "resolved_by_experiment": "exp_bnd_1"},
    ]
    assert f"[BOUNDARY] resolved curiosity {cur_bnd} by exp_bnd_1" in out
    scopes = _rows(db, "SELECT claim_id, scope_text, source_experiment,"
                       " source_curiosity_id FROM claim_scopes")
    assert scopes == [{"claim_id": 4242, "scope_text": f_bnd.strip()[:1200],
                       "source_experiment": "exp_bnd_1",
                       "source_curiosity_id": cur_bnd}]
    assert "[SCOPE] recorded mapped regime for claim 4242 from exp_bnd_1" in out


def test_benchmark_lineage_bypass_and_dead_benchmark_propagation(env, capsys):
    """A benchmark parent makes queue children bypass the evidence gate with
    benchmark provenance and suppresses confirmed-branching. Pins the DEAD
    parent_benchmark_id propagation as dead: experiments.benchmark_id and
    worker_results.benchmark_id stay NULL because the parent fetch happens
    AFTER the experiments insert and 1a-bis. Also pins [QA-SKIP] (too few
    words) and [QA-DEDUP] (0.55 Jaccard vs an active curiosity)."""
    db = env["db"]
    decoy = ("Characterize oxide breakdown voltage drift across accelerated "
             "humidity stress conditions")
    _seed_curiosity(db, decoy)
    parent = _seed_curiosity(
        db, "Benchmark question: does annealing time correlate with defect "
            "density in HVM lots?",
        benchmark_id="bench3_q7", known_answer="42")
    hyp = ("Annealing time correlates with defect density across production "
           "lots")
    q_good = ("Does defect density track anneal ramp rate across equipment "
              "vendors and lot sizes?")
    q_short = "ab cd ef gh ij kl"
    q_dup = decoy + " today"
    _seed_task(env["home"], "task-bench-1", "exp_bench_1: benchmark",
               f"HYPOTHESIS: {hyp}\nCURIOSITY_ID: {parent}")
    wr = _seed_worker_result(
        db, experiment_id="exp_bench_1", kanban_task_id="task-bench-1",
        hypothesis_supported=1,
        key_finding="CONFIRMED: Anneal time correlates with defect density "
                    "(r=0.62, n=200 lots) because longer anneals shrink void "
                    "nucleation sites.",
        confidence=0.85, domain=DOMAIN_A,
        queue_additions="; ".join([q_good, q_short, q_dup]),
        worker_id="worker-bench")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 1
    # Benchmark child: gate bypassed (wsc 0.0 would block a plain follow-up)
    child = _rows(db, "SELECT text, priority, status, source_experiment,"
                      " parent_curiosity_id, provenance, benchmark_id,"
                      " known_answer FROM curiosities"
                      " WHERE text = ?", (q_good,))
    assert child == [{
        "text": q_good, "priority": 3, "status": "active",
        "source_experiment": "exp_bench_1", "parent_curiosity_id": parent,
        "provenance": json.dumps({"parser": "new", "origin": "benchmark",
                                  "interventions": ["I6"], "depth": 0}),
        # benchmark_id/known_answer do NOT propagate to children
        "benchmark_id": None, "known_answer": None,
    }]
    assert "[QA-GATE]" not in out
    assert f"[QA-SKIP] too few words: '{q_short[:60]}'" in out
    assert f"[QA-DEDUP] duplicate skipped: '{q_dup[:60]}'" in out
    # Benchmark parent suppresses confirmed-branching entirely
    assert "[CF-INSERT]" not in out
    # Parent resolved by its experiment
    prow = _rows(db, "SELECT status, source_experiment,"
                     " resolved_by_experiment FROM curiosities WHERE id = ?",
                 (parent,))[0]
    assert prow == {"status": "resolved", "source_experiment": "exp_bench_1",
                    "resolved_by_experiment": "exp_bench_1"}
    # DEAD propagation pinned dead: benchmark_id fetched only AFTER the
    # experiments insert + 1a-bis, so neither row carries it.
    assert _rows(db, "SELECT benchmark_id FROM experiments") == [
        {"benchmark_id": None}]
    assert _rows(db, "SELECT benchmark_id FROM worker_results WHERE id = ?",
                 (wr,)) == [{"benchmark_id": None}]


def test_generation_throttle_and_deep_lineage_exemptions(env, capsys,
                                                         monkeypatch):
    """should_throttle stubbed True everywhere: [TRANSFER] queue items are
    throttled before insert (no transfer_tracking row), refutation branching
    hits its quota print, confirmed branching is skipped SILENTLY, Method-4
    root curiosities are NOT throttled, and a deep parent (evidence_depth>=8)
    is exempt from BOTH the evidence gate and the throttle."""
    import generation_throttle
    monkeypatch.setattr(generation_throttle, "should_throttle",
                        lambda conn=None, source=None: True)
    db = env["db"]
    hyp_r = ("Optical calibration drift accumulates linearly across "
             "interferometer restarts")
    hyp_c = ("Batched writes amortize fsync latency for append-heavy "
             "workloads")
    hyp_deep = ("Depth-nine lineage hypothesis about vibration damping in "
                "cryogenic mounts")
    deep_parent = _seed_curiosity(
        db, "Deep lineage parent question about cryogenic vibration damping "
            "isolation stages", evidence_depth=9)
    _seed_task(env["home"], "task-thr-r", "exp_thr_r: drift",
               f"HYPOTHESIS: {hyp_r}")
    _seed_task(env["home"], "task-thr-c", "exp_thr_c: fsync",
               f"HYPOTHESIS: {hyp_c}")
    _seed_task(env["home"], "task-thr-d", "exp_thr_d: deep",
               f"HYPOTHESIS: {hyp_deep}\nCURIOSITY_ID: {deep_parent}")
    q_deep = ("Does damping stage count change the resonance floor across "
              "cryostat vendors and mounting loads?")
    _seed_worker_result(
        db, experiment_id="exp_thr_r", kanban_task_id="task-thr-r",
        hypothesis_supported=0,
        key_finding="REFUTED: Drift was sublinear (0.3x per restart, n=22) "
                    "because the servo re-zeroes between runs.",
        confidence=0.8, domain=DOMAIN_A,
        queue_additions="[TRANSFER from optics] Does calibration drift "
                        "persist across spectrometer restarts?",
        worker_id="worker-thr-1")
    _seed_worker_result(
        db, experiment_id="exp_thr_c", kanban_task_id="task-thr-c",
        hypothesis_supported=1,
        key_finding="CONFIRMED: Batching cut fsync stalls 41% (n=30 runs) "
                    "because group commit amortized the barrier.",
        confidence=0.85, domain=DOMAIN_B, worker_id="worker-thr-2")
    _seed_worker_result(
        db, experiment_id="exp_thr_d", kanban_task_id="task-thr-d",
        hypothesis_supported=1,
        key_finding="CONFIRMED: Adding a third damping stage lowered the "
                    "resonance floor 2.4x (n=15 mounts) because series "
                    "isolation multiplies attenuation.",
        confidence=0.85, domain=DOMAIN_A, queue_additions=q_deep,
        worker_id="worker-thr-3")

    applied = awr.apply_results()
    out = capsys.readouterr().out

    assert applied == 3
    assert out.count("[THROTTLE] Generation cap reached") == 3
    assert out.count("[QA-THROTTLE] throttle blocked") == 1
    assert out.count("[RF-THROTTLE] refutation_followup quota reached") == 1
    # Confirmed branching is skipped silently while throttled
    assert "[CF-INSERT]" not in out
    assert "[RF-INSERT]" not in out
    # The throttled [TRANSFER] item never landed -> no tracking row either
    assert _rows(db, "SELECT * FROM transfer_tracking") == []
    # Deep lineage (depth 9): exempt from gate AND throttle -> lands
    assert out.count("[QA-INSERT]") == 1
    deep_child = _rows(db, "SELECT text, parent_curiosity_id, provenance"
                           " FROM curiosities WHERE text = ?", (q_deep,))
    assert deep_child == [{
        "text": q_deep, "parent_curiosity_id": deep_parent,
        "provenance": json.dumps({
            "parser": "new", "origin": "follow_up", "interventions": ["I6"],
            "parent_source": "exp_thr_d", "created_at_fix": True}),
    }]
    # Method-4 root curiosities are NOT throttled (unconditional lineage)
    roots = _rows(db, "SELECT text, source_experiment FROM curiosities"
                      " WHERE parent_curiosity_id IS NULL AND provenance"
                      " IS NULL AND id != ? ORDER BY id", (deep_parent,))
    assert roots == [
        {"text": hyp_r, "source_experiment": "exp_thr_r"},
        {"text": hyp_c, "source_experiment": "exp_thr_c"},
    ]
    assert out.count("[LINEAGE] Created root curiosity") == 2


def test_lineage_live_cross_domain_and_transfer_completion(env, capsys):
    """The child-side lineage_live detector: when the parent curiosity's
    source experiment lives in a different domain, a lineage_live
    transfer_tracking row is inserted. Also pins update_completed(conn=)
    flipping a queued transfer row for this task, and that a parent with a
    pre-set source_experiment is NOT resolved by the intake."""
    db = env["db"]
    _seed_experiment(db, "exp_parent_lin", "materials_science",
                     hypothesis="Parent annealing hypothesis",
                     result="CONFIRMED: grain growth mechanism (n=40)")
    parent = _seed_curiosity(
        db, "How does annealing-driven grain growth change creep resistance "
            "in nickel superalloys?", source_experiment="exp_parent_lin")
    hyp = ("Grain boundary sliding dominates creep at low stress in "
           "fine-grained alloys")
    _seed_task(env["home"], "task-lin-1", "exp_lin_1: creep",
               f"HYPOTHESIS: {hyp}\nCURIOSITY_ID: {parent}")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO transfer_tracking (source_result_id, source_domain,"
        " target_domain, task_id, status, created_at, transfer_source)"
        " VALUES (90909, 'materials_science', ?, 'task-lin-1', 'queued', ?,"
        " 'explicit_transfer')",
        (DOMAIN_A, float(WR_CREATED_AT - 3600)))
    conn.commit()
    conn.close()
    wr = _seed_worker_result(
        db, experiment_id="exp_lin_1", kanban_task_id="task-lin-1",
        hypothesis_supported=1,
        key_finding="CONFIRMED: Sliding accounted for 71% of creep strain "
                    "(n=18 alloys) because fine grains multiply boundary "
                    "area.",
        confidence=0.8, domain=DOMAIN_A, worker_id="worker-lin")

    applied = awr.apply_results()

    assert applied == 1
    transfers = _rows(db, "SELECT source_result_id, source_domain,"
                          " target_domain, task_id, destination_result_id,"
                          " destination_domain, status, transfer_source"
                          " FROM transfer_tracking ORDER BY id")
    assert transfers == [
        {"source_result_id": 90909, "source_domain": "materials_science",
         "target_domain": DOMAIN_A, "task_id": "task-lin-1",
         "destination_result_id": wr, "destination_domain": DOMAIN_A,
         "status": "completed_cross_domain",
         "transfer_source": "explicit_transfer"},
        {"source_result_id": wr, "source_domain": "materials_science",
         "target_domain": DOMAIN_A, "task_id": None,
         "destination_result_id": None, "destination_domain": None,
         "status": "queued", "transfer_source": "lineage_live"},
    ]
    completed = _rows(db, "SELECT task_completed_at FROM transfer_tracking"
                          " WHERE task_id = 'task-lin-1'")[0]
    assert completed["task_completed_at"] and completed["task_completed_at"] > 0
    # Parent already had source_experiment -> intake does NOT resolve it
    prow = _rows(db, "SELECT status, source_experiment,"
                     " resolved_by_experiment FROM curiosities WHERE id = ?",
                 (parent,))[0]
    assert prow == {"status": "active",
                    "source_experiment": "exp_parent_lin",
                    "resolved_by_experiment": None}


def test_junk_domain_embedding_stub_classification(env, capsys, monkeypatch):
    """An empty worker domain routes through classify_embedding (stubbed via
    sys.modules — the real module would call the embed server). The
    classified domain lands on the experiment/manifest, while
    worker_results.domain is deliberately NOT synced (the sync only covers
    the domain-creation-gate change, captured AFTER classification)."""
    import types

    fake = types.ModuleType("embedding_domain_classifier")
    fake.classify_embedding = lambda text: ("stub_target_domain", 0.91)
    monkeypatch.setitem(sys.modules, "embedding_domain_classifier", fake)

    db = env["db"]
    hyp = ("Resonant mode splitting predicts coupling strength in paired "
           "microcavities")
    _seed_task(env["home"], "task-junk-1", "exp_junk_1: cavities",
               f"HYPOTHESIS: {hyp}")
    wr = _seed_worker_result(
        db, experiment_id="exp_junk_1", kanban_task_id="task-junk-1",
        hypothesis_supported=1,
        key_finding="CONFIRMED: Mode splitting tracked coupling strength "
                    "(r=0.88, n=60 cavity pairs) because hybridized modes "
                    "share the photon exchange rate.",
        confidence=0.85, domain="", worker_id="worker-junk")

    applied = awr.apply_results()

    assert applied == 1
    assert _rows(db, "SELECT domain FROM experiments") == [
        {"domain": "stub_target_domain"}]
    # The raw worker row keeps its junk domain: the worker_results sync only
    # fires when the domain-creation GATE changes the value, not the
    # classifier (original_domain is captured after classification).
    assert _rows(db, "SELECT domain FROM worker_results WHERE id = ?",
                 (wr,)) == [{"domain": ""}]
    manifest = json.loads(
        (env["home"] / "artifacts" / "task-junk-1" / "manifest.json")
        .read_text())
    assert manifest["domain"] == "stub_target_domain"
