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

Known coverage gaps (paths that need heavier scaffolding or live services):
  - adversarial-replication / dispute-arbitration / retest-credit /
    boundary-curiosity marker routing (1e/1f/1g)
  - benchmark lineage propagation (parent_benchmark_id)
  - junk-domain auto-classification via the embedding server (fixture rows
    always carry a clean domain so classify_embedding is never imported)
  - the authority-basis confidence cap (currently dead code in this repo:
    write_worker_result has no parse_basis, so the import always fails) and
    the caveat confidence cap
  - generation-throttle-active branches; dedup-set overflow (>10k rows);
    state_write_lock timeout (yields None); drift detector internals
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


def _seed_curiosity(db, text):
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO curiosities (text, priority, status, created_at) "
        "VALUES (?, 5, 'active', ?)", (text, float(WR_CREATED_AT - 3600)))
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
