#!/usr/bin/env python3
"""
apply_worker_results.py — Apply structured worker results to state.

Reads new entries from worker_results table (applied=0), updates:
  1. experiments table in prometheus.db
  2. self_state.json (experiments_completed_list, curiosity_queue, counters)

Run every 2-5 minutes via cron. Replaces the synthesis worker bottleneck
for basic state updates.

Workers write structured results via write_worker_result.py.
This script applies them atomically.

Uses state_write_lock for serialized access (prevents race conditions
with synthesis_merger and sync_sqlite_to_state).
"""
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

# Canonical main hermes dir — derived from script location to avoid HOME override issues

"""Apply Worker Results.

Part of the Prometheus research infrastructure.
"""

_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(os.path.join(_hermes_home_env, "prometheus_db.py")) else _HERMES_MAIN

# Use shared state_lock for serialized write access (prevents race conditions)
sys.path.insert(0, HERMES_HOME)
from state_lock import load_state, save_state, state_write_lock
from quality_validator import validate_quality
from prometheus_db import get_db
from db_retry import get_db as retry_get_db
from transfer_tracking import insert_tracking, update_completed, parse_transfer_target


# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default

DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")
STATE_PATH = os.path.join(HERMES_HOME, "self_state.json")


def _parse_tags(raw):
    """Format-tolerant tag parser.

    ROOT-CAUSE FIX (2026-06-08): worker writers (write_worker_result.py) store
    tags as a comma-separated plain string (e.g. "CONFIRMED,generalization").
    The previous reader used json.loads() only, which raises on non-JSON input
    and silently fell back to [] — wiping tags on ~60% of experiments. This
    accepts BOTH a JSON array string ('["A","B"]') and comma/semicolon-separated
    plain text, returning a clean list of non-empty tag strings.
    """
    if not raw or not str(raw).strip():
        return []
    s = str(raw).strip()
    if s.startswith('['):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(t).strip() for t in v if str(t).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [t.strip() for t in s.replace(';', ',').split(',') if t.strip()]


# Maps a computed verdict to a canonical verdict tag. Guarantees that an
# experiment with a known verdict is never left fully untagged even if the
# worker supplied no parseable tags.
_VERDICT_TO_TAG = {
    'REFUTED': 'REFUTED',
    'PARTIALLY REFUTED': 'PARTIAL',
    'PARTIAL': 'PARTIAL',
    'REFUTED_SETUP': 'REFUTED',
    'SUPPORTED': 'SUPPORTED',
    'CONFIRMED': 'SUPPORTED',
}


def normalize_domain(domain):
    """Normalize a domain string to canonical form.

    Delegates to write_worker_result.normalize_domain — the single source of
    truth. This module used to carry its own stale copy that still contained
    the greedy/lossy mappings ('general'->'calibration', 'rlhf'->'calibration',
    'rag*'->'injection_detection', 'defense'/'attack', 'synthesis'/'meta', ...)
    removed there on 2026-06-08 as the ROOT CAUSE of domain mislabeling — so
    the apply stage silently re-applied the exact merges the write stage
    banned, fighting the embedding classifier. Do not re-inline a dict here.
    """
    from write_worker_result import normalize_domain as _canonical
    return _canonical(domain)



def _extract_curiosity_id_from_body(body):
    """Extract CURIOSITY_ID from a kanban task body."""
    if not body:
        return None
    for line in body.split("\n"):
        if line.strip().startswith("CURIOSITY_ID:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                return None
    return None


def _extract_curiosity_id_from_task(kanban_task_id):
    """Get curiosity_id from the kanban task body."""
    if not kanban_task_id:
        return None
    kanban_db = os.path.join(HERMES_HOME, "kanban.db")
    try:
        kconn = retry_get_db(kanban_db)
        row = kconn.execute("SELECT body FROM tasks WHERE id = ?", (kanban_task_id,)).fetchone()
        kconn.close()
        if row and row[0]:
            return _extract_curiosity_id_from_body(row[0])
    except Exception:
        pass
    return None


def _extract_hyp_from_body(body):
    """Extract hypothesis text from a kanban task body.

    Handles multiple body formats:
      - "HYPOTHESIS: <text>"
      - "EXPERIMENT: <text>"
      - "INVESTIGATION: exp_NNN: <text>"
      - "INVESTIGATION: <text>"
      - "Hypothesis: <text>" (case-insensitive, after blank lines)
    Returns the hypothesis string, or None if not found.
    """
    if not body:
        return None
    for line in body.split("\n"):
        stripped = line.strip()
        # "HYPOTHESIS: Can X predict Y?"
        if stripped.upper().startswith("HYPOTHESIS:"):
            return stripped.split(":", 1)[1].strip().rstrip("\\")
        # "EXPERIMENT: Can X predict Y?"
        if stripped.upper().startswith("EXPERIMENT:"):
            return stripped.split(":", 1)[1].strip().rstrip("\\")
        # "INVESTIGATION: exp_72527525: Can X predict Y?"
        if stripped.upper().startswith("INVESTIGATION:"):
            after = stripped.split(":", 1)[1].strip()
            # Strip experiment ID prefix if present (e.g. "exp_72527525: ...")
            m = re.match(r'exp_\d+:\s*(.+)', after)
            if m:
                return m.group(1).strip().rstrip("\\")
            return after.rstrip("\\")
        # "INVESTIGATION TASK: exp_NNN\nHYPOTHESIS: <text>"
        # (handled by HYPOTHESIS check above on next line)
    # Second pass: look for "Hypothesis: <text>" (capitalized, not all-caps)
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith("hypothesis:") and not stripped.upper().startswith("HYPOTHESIS:"):
            return stripped.split(":", 1)[1].strip().rstrip("\\")
    return None


def _extract_marker_id(kanban_task_id, key_finding, pat):
    """Extract an integer claim-id marker from the finding or the task body.

    Used for ADVERSARIAL_REPLICATION_FOR_CLAIM (adversarial_replication_enqueuer)
    and DISPUTE_ARBITRATION_FOR_CLAIM (dispute_arbitration_enqueuer) routing."""
    if key_finding:
        m = re.search(pat, key_finding)
        if m:
            return int(m.group(1))
    if kanban_task_id:
        kanban_db = os.path.join(HERMES_HOME, "kanban.db")
        try:
            kconn = retry_get_db(kanban_db)
            row = kconn.execute("SELECT body FROM tasks WHERE id = ?",
                                (kanban_task_id,)).fetchone()
            kconn.close()
            if row and row[0]:
                m = re.search(pat, row[0])
                if m:
                    return int(m.group(1))
        except Exception:
            pass
    return None


def _extract_adv_claim_id(kanban_task_id, key_finding):
    return _extract_marker_id(kanban_task_id, key_finding,
                              r"ADVERSARIAL_REPLICATION_FOR_CLAIM:\s*(\d+)")


def get_original_hypothesis(kanban_task_id, experiment_id=None):
    """Extract original hypothesis from kanban task body.

    Tries kanban_task_id first, then falls back to searching by experiment ID in title.
    """
    kanban_db = os.path.join(HERMES_HOME, "kanban.db")

    # Try by task ID first
    if kanban_task_id:
        try:
            kconn = retry_get_db(kanban_db)
            row = kconn.execute(
                "SELECT body FROM tasks WHERE id = ?", (kanban_task_id,)
            ).fetchone()
            kconn.close()
            if row and row[0]:
                hyp = _extract_hyp_from_body(row[0])
                if hyp:
                    return hyp
        except Exception:
            pass

    # Fallback: search by experiment ID in task title
    if experiment_id:
        try:
            kconn = retry_get_db(kanban_db)
            row = kconn.execute(
                "SELECT body FROM tasks WHERE title LIKE ? LIMIT 1",
                (f"{experiment_id}%",)
            ).fetchone()
            kconn.close()
            if row and row[0]:
                hyp = _extract_hyp_from_body(row[0])
                if hyp:
                    return hyp
        except Exception:
            pass

    return None


@dataclass
class _ApplyContext:
    """Mutable shared state for one apply_results() batch.

    Built once at the top of apply_results() and threaded through every
    _stage_* helper below. It carries exactly the locals the original
    monolithic loop shared between its sequential sections:

      batch scope — conn, state (the state_write_lock dict), the pre-loaded
          dedup sets, the applied counter, and should_throttle (the
          function-local import binding made by the throttle stage and
          referenced later by the branching stages; kept on ctx so a failed
          import keeps failing open exactly like the old unbound local).
      row scope — the raw worker_results columns (bound by _begin_result)
          plus the values derived stage by stage (verdict, timestamps,
          hypothesis, quality, lineage, throttle, inserted).

    Stages read ctx fields into locals under the ORIGINAL variable names,
    run the moved body verbatim, and write rebound outputs back; in-place
    mutations (tags list, state dict, dedup sets) are shared by reference.
    Row-scope fields are NOT reset between rows — the original locals
    persisted across loop iterations the same way, and every read is
    preceded by a same-row write.
    """
    # ── batch scope ──
    conn: object = None
    state: object = None
    existing_curiosities: object = None
    completed_hyps: object = None
    applied: int = 0
    should_throttle: object = None
    # ── row scope: raw worker_results columns ──
    result_id: object = None
    exp_id: object = None
    task_id: object = None
    hyp_supported: object = None
    key_finding: object = None
    confidence: object = None
    domain: object = None
    tags_json: object = None
    files_json: object = None
    queue_json: object = None
    worker_id: object = None
    created_at: object = None
    pred_dir_json: object = None
    obs_dir_json: object = None
    design_vec_json: object = None
    experiment_type: object = None
    mechanism_type: object = None
    wr_model: object = None
    calibrated_confidence: object = None
    wr_verdict_basis: object = None
    # ── row scope: derived per stage ──
    tags: object = None
    files: object = None
    queue_additions: object = None
    result_text: object = None
    verdict: object = None
    now_unix: object = None
    created_unix: object = None
    original_hyp: object = None
    hyp_to_store: object = None
    quality_score: object = None
    quality_tags: object = None
    reject: object = None
    parent_curiosity_id: object = None
    parent_benchmark_id: object = None
    parent_known_answer: object = None
    parent_depth: object = 0
    throttle_active: object = False
    parent_source: object = None
    inserted: object = 0


def _load_dedup_sets(ctx):
    """Pre-load dedup sets ONCE before the loop (was per-result = O(n*30K)
    scans).
    """
    conn = ctx.conn

    existing_curiosities = set()
    try:
        cur_rows = conn.execute(
            "SELECT text FROM curiosities WHERE status='active'"
        ).fetchall()
        for (text,) in cur_rows:
            if text and len(text) > 10:
                existing_curiosities.add(text.lower())
    except Exception:
        pass

    completed_hyps = set()
    try:
        hyp_rows = conn.execute(
            "SELECT hypothesis FROM experiments WHERE status='completed' AND hypothesis IS NOT NULL"
        ).fetchall()
        for (hyp,) in hyp_rows:
            if hyp and len(hyp) > 20:
                completed_hyps.add(hyp.lower())
    except Exception:
        pass

    ctx.existing_curiosities = existing_curiosities
    ctx.completed_hyps = completed_hyps


def _begin_result(ctx, row):
    """Bind one worker_results row onto the per-result context fields."""
    (ctx.result_id, ctx.exp_id, ctx.task_id, ctx.hyp_supported,
     ctx.key_finding, ctx.confidence, ctx.domain, ctx.tags_json,
     ctx.files_json, ctx.queue_json, ctx.worker_id, ctx.created_at,
     ctx.pred_dir_json, ctx.obs_dir_json, ctx.design_vec_json,
     ctx.experiment_type, ctx.mechanism_type, ctx.wr_model,
     ctx.calibrated_confidence, ctx.wr_verdict_basis) = row


def _stage_parse_fields(ctx):
    """Parse the JSON-ish worker_results columns (tags / files /
    queue_additions).
    """
    tags_json = ctx.tags_json
    files_json = ctx.files_json
    queue_json = ctx.queue_json

    # Parse JSON fields (handle malformed JSON gracefully)
    # ROOT-CAUSE FIX (2026-06-08): worker writers store tags as a
    # comma-separated plain string (write_worker_result.py), but this
    # reader previously used json.loads() ONLY, so every comma-form tag
    # set silently became []. ~9,786 experiments lost their tags this way.
    # _parse_tags() now accepts BOTH JSON-array and comma/semicolon form.
    tags = _parse_tags(tags_json)
    try:
        files = json.loads(files_json) if files_json and files_json.strip() else []
    except (json.JSONDecodeError, TypeError):
        files = []
    try:
        if queue_json and queue_json.strip():
            if queue_json.strip().startswith('["'):
                # Valid JSON array of strings (e.g., ["question1", "question2"])
                queue_additions = json.loads(queue_json)
            elif queue_json.strip().startswith('['):
                # Text that happens to start with '[' (e.g., [TRANSFER] questions)
                # These are NOT JSON — treat as semicolon-separated or single item
                if ';' in queue_json:
                    queue_additions = [q.strip() for q in queue_json.split(';') if q.strip()]
                else:
                    queue_additions = [queue_json.strip()]
            else:
                # Semicolon-separated format
                queue_additions = [q.strip() for q in queue_json.split(';') if q.strip()]
        else:
            queue_additions = []
    except (json.JSONDecodeError, TypeError):
        queue_additions = []

    ctx.tags = tags
    ctx.files = files
    ctx.queue_additions = queue_additions


def _stage_shadow_parser(ctx):
    """Shadow-parser audit (prints only; no state changes)."""
    queue_json = ctx.queue_json
    queue_additions = ctx.queue_additions

    # ── Shadow Parser (I6 isolation, audit framework Tier 1) ──
    # Runs OLD parser logic alongside NEW to measure Parser Loss Rate (PLR)
    # and Parser Recovery Rate (PRR). The old parser assumed any string
    # starting with '[' was JSON, and silently dropped non-JSON strings.
    # This is a deterministic pure-function comparison — no policy/distribution confound.
    # PLR = Old_Drop_Events / Parser_Input_Events (pure parser rate, not pipeline-conditioned)
    try:
        _parser_input_count = 0
        _old_drop_count = 0
        _recovery_count = 0

        _old_parsed = []
        if queue_json and queue_json.strip():
            if queue_json.strip().startswith('['):
                try:
                    _old_parsed = json.loads(queue_json)
                except (json.JSONDecodeError, TypeError):
                    _old_parsed = []  # OLD BEHAVIOR: silently drop
            else:
                _old_parsed = [q.strip() for q in queue_json.split(';') if q.strip()]

        _new_set = set(queue_additions)
        _old_set = set(_old_parsed)
        _drop_set = _old_set - _new_set    # items old kept but new drops (should be empty)
        _recovery_set = _new_set - _old_set  # items new keeps but old dropped (THE KEY METRIC)

        # Count parser input events (correct PLR denominator)
        _parser_input_count = len(queue_additions) if queue_additions else 0
        _recovery_count = len(_recovery_set)
        _old_drop_count = len(_recovery_set)  # old dropped these = new recovered these

        if _recovery_set:
            # These are items the OLD parser would have silently destroyed
            print(f"  [SHADOW-PARSER] INPUT={_parser_input_count} RECOVERY={_recovery_count} PLR={_old_drop_count}/{_parser_input_count}={_old_drop_count/max(_parser_input_count,1):.2%}")
            for item in _recovery_set:
                print(f"    [RECOVERED] '{item[:80]}'")
        elif _parser_input_count > 0:
            print(f"  [SHADOW-PARSER] INPUT={_parser_input_count} RECOVERY=0 PLR=0/{_parser_input_count}=0.00%")
        if _drop_set:
            print(f"  [SHADOW-PARSER] DROP: {len(_drop_set)} items new parser drops but old kept")
            for item in _drop_set:
                print(f"    [DROPPED] '{item[:80]}'")
    except Exception:
        pass


def _stage_derive_verdict(ctx):
    """Derive the verdict (finding text first, --supported flag as fallback)
    and inject the verdict prefix into the result text when missing.
    """
    key_finding = ctx.key_finding
    hyp_supported = ctx.hyp_supported

    # Build result text from key_finding
    # Source of truth: the finding text, not the hypothesis_supported flag.
    # Workers sometimes pass --supported while writing REFUTED in the text.
    result_text = key_finding or ""

    # Derive verdict from the finding text first (authoritative)
    text_verdict = None
    if result_text:
        # Match verdict patterns at the start of the finding
        # Handles optional experiment ID prefix in multiple formats:
        #   "exp_3523 CONFIRMED: ..." (space-separated)
        #   "exp_3797: CONFIRMED ..." (colon-separated)
        # Order matters: more specific patterns first
        _vm = re.match(
            r'^\s*(?:exp_\d+\s*:\s*|exp_\d+\s+)?'
            r'(PARTIALLY\s+REFUTED|HYPOTHESIS\s+REFUTED|'
            r'PARTIALLY[-_\s]CONFIRMED|PARTIALLY[-_]SUPPORTED|PARTIAL[-_]CONFIRMED|'
            r'PARTIAL\s+SUPPORT(?:ED)?|'  # handles "PARTIAL SUPPORT" and "PARTIAL SUPPORTED"
            r'CONFIRMED|PARTIALLY\s+SUPPORTED|SUPPORTED|'
            r'MECHANISM\s+CONFIRMED|'
            r'REFUTED_SETUP|REFUTED_HYPOTHESIS|'  # disambiguated refutation types
            r'REFUTED|'
            r'HYPOTHESIS\s+PARTIALLY\s+CONFIRMED|'
            r'HYPOTHESIS\s+CONFIRMED|HYPOTHESIS\s+SUPPORTED)'
            r'\s*[:\-]?',
            result_text, re.IGNORECASE
        )
        if _vm:
            raw = _vm.group(1).upper().strip().replace('-', ' ')
            # Normalize to canonical forms
            if 'REFUTED_SETUP' in raw:
                text_verdict = 'REFUTED_SETUP'  # Experimental setup couldn't test hypothesis
            elif 'REFUTED_HYPOTHESIS' in raw:
                text_verdict = 'REFUTED'  # Hypothesis itself was tested and failed
            elif 'REFUTED' in raw:
                text_verdict = 'REFUTED' if 'PARTIALLY' not in raw else 'PARTIALLY REFUTED'
            elif 'CONFIRMED' in raw or 'SUPPORTED' in raw:
                text_verdict = 'SUPPORTED'

    # Fall back to flag-derived verdict only if text has no signal
    if text_verdict:
        verdict = text_verdict
    elif hyp_supported is not None:
        verdict = "SUPPORTED" if hyp_supported else "REFUTED"
    else:
        verdict = None

    if verdict:
        # Don't double-prefix: skip if text already starts with the verdict
        # Also handles optional experiment ID prefix (e.g. "exp_3523 CONFIRMED:")
        if not re.match(r'^\s*(?:exp_\d+\s*:\s*|exp_\d+\s+)?(HYPOTHESIS\s+)?(SUPPORTED|REFUTED|CONFIRMED|PARTIALLY)', result_text, re.IGNORECASE):
            result_text = f"{verdict}: {result_text}"

    ctx.result_text = result_text
    ctx.verdict = verdict


def _stage_resolve_timestamps(ctx):
    """Resolve now/created timestamps (ISO strings and UNIX floats)."""
    created_at = ctx.created_at

    # Use current timestamp as created_at fallback (UNIX float)
    now_unix = datetime.now(timezone.utc).timestamp()
    ts = created_at or now_unix

    # Convert to float — handle both ISO strings and UNIX floats
    try:
        if isinstance(ts, (int, float)):
            created_unix = float(ts)
        else:
            ts_str = str(ts).replace("Z", "+00:00")
            if "+" not in ts_str and not ts_str.endswith("+00:00"):
                ts_str += "+00:00"
            dt = datetime.fromisoformat(ts_str)
            created_unix = dt.timestamp()
    except Exception:
        created_unix = datetime.now(timezone.utc).timestamp()

    ctx.now_unix = now_unix
    ctx.created_unix = created_unix


def _stage_resolve_hypothesis(ctx):
    """Recover the original hypothesis from the kanban task body."""
    task_id = ctx.task_id
    exp_id = ctx.exp_id
    key_finding = ctx.key_finding

    # 1. Update experiments table (including epistemic fields)
    # Look up original hypothesis from kanban task body
    original_hyp = get_original_hypothesis(task_id, exp_id)
    hyp_to_store = original_hyp or key_finding or exp_id

    ctx.original_hyp = original_hyp
    ctx.hyp_to_store = hyp_to_store


def _stage_confidence_backstops(ctx):
    """Intake-side confidence caps: the verdict-basis (authority) backstop
    and the blocker-caveat clamp. Both are protective, never blocking.
    """
    conn = ctx.conn
    result_id = ctx.result_id
    exp_id = ctx.exp_id
    key_finding = ctx.key_finding
    wr_verdict_basis = ctx.wr_verdict_basis
    confidence = ctx.confidence
    tags = ctx.tags

    # Verdict-basis backstop (2026-07-01): results written before
    # the --basis flag existed (or via the bridge) may carry a
    # "BASIS: x" line only in the finding text. Parse it, persist
    # it, and cap authority-based confidence at 0.5 — trusting a
    # source is a report, not a verification.
    try:
        from write_worker_result import parse_basis, AUTHORITY_CONFIDENCE_CAP
        _basis = parse_basis(key_finding, wr_verdict_basis)
        if _basis and _basis != wr_verdict_basis:
            wr_verdict_basis = _basis
            conn.execute(
                "UPDATE worker_results SET verdict_basis = ? WHERE id = ?",
                (_basis, result_id))
        if (wr_verdict_basis == 'literature_authority'
                and confidence is not None
                and float(confidence) > AUTHORITY_CONFIDENCE_CAP):
            print(f"  AUTHORITY CAP: {exp_id} confidence "
                  f"{float(confidence):.2f} -> {AUTHORITY_CONFIDENCE_CAP}")
            confidence = AUTHORITY_CONFIDENCE_CAP
            if "AUTHORITY_BASIS" not in tags:
                tags.append("AUTHORITY_BASIS")
            conn.execute(
                "UPDATE worker_results SET confidence = ?, "
                "calibrated_confidence = min(COALESCE(calibrated_confidence, ?), ?) "
                "WHERE id = ?",
                (confidence, confidence, confidence, result_id))
    except Exception:
        pass  # basis handling is protective, never blocking

    # Blocker-caveat confidence clamp (2026-07-01): if the finding
    # itself admits no independent verification, cap confidence at
    # 0.6 before it feeds the gate, claim evidence, and
    # confidence_change. Intake-side backstop for results written
    # before the write-time cap existed and for bridge-recovered
    # results that bypass write_worker_result.py.
    try:
        from quality_validator import caveat_confidence_cap
        _capped_conf, _caveat_hits = caveat_confidence_cap(key_finding, confidence)
        if _caveat_hits and _capped_conf != confidence:
            print(f"  CAVEAT CAP: {exp_id} confidence "
                  f"{float(confidence):.2f} -> {_capped_conf:.2f} "
                  f"(\"{_caveat_hits[0][:60]}\")")
            confidence = _capped_conf
            if "CAVEAT_CONF_CAPPED" not in tags:
                tags.append("CAVEAT_CONF_CAPPED")
            conn.execute(
                "UPDATE worker_results SET confidence = ?, "
                "calibrated_confidence = min(COALESCE(calibrated_confidence, ?), ?), "
                "tags = CASE WHEN COALESCE(tags,'') = '' THEN 'CAVEAT_CONF_CAPPED' "
                "WHEN instr(tags,'CAVEAT_CONF_CAPPED') = 0 THEN tags || ',CAVEAT_CONF_CAPPED' "
                "ELSE tags END WHERE id = ?",
                (confidence, confidence, confidence, result_id)
            )
    except Exception:
        pass  # clamp is protective, never blocking

    ctx.wr_verdict_basis = wr_verdict_basis
    ctx.confidence = confidence


def _stage_validate_quality(ctx):
    """Score result quality and set the reject flag."""
    hyp_to_store = ctx.hyp_to_store
    key_finding = ctx.key_finding
    confidence = ctx.confidence
    exp_id = ctx.exp_id

    # Validate quality before writing
    quality_score, quality_tags, _reject = validate_quality(
        hyp_to_store, key_finding, confidence, exp_id
    )
    reject = quality_score < 40

    ctx.quality_score = quality_score
    ctx.quality_tags = quality_tags
    ctx.reject = reject


def _stage_reject_low_quality(ctx):
    """Reject path. Returns True when the result was swallowed (the caller
    must `continue` to the next row — the extracted form of the old
    per-result `continue`), False to proceed down the pipeline.
    """
    conn = ctx.conn
    state = ctx.state
    result_id = ctx.result_id
    exp_id = ctx.exp_id
    quality_score = ctx.quality_score
    quality_tags = ctx.quality_tags
    reject = ctx.reject

    # Skip rejected results — don't write zombie experiments
    if reject:
        print(f"  REJECTED [{quality_score}]: {exp_id} — {','.join(quality_tags[:3])}")
        print(f"    Result not written to experiments table (quality too low)")
        # Still update self_state counters so worker isn't stuck
        if state:
            ecl = state.get("experiments_completed_list", [])
            if exp_id not in ecl:
                ecl.append(exp_id)
                state["experiments_completed_list"] = ecl
            state["metrics"] = state.get("metrics", {})
            state["metrics"]["experiments_run"] = state["metrics"].get("experiments_run", 0) + 1
            state["metrics"]["hypotheses_tested"] = state["metrics"].get("hypotheses_tested", 0) + 1
        conn.execute("UPDATE worker_results SET applied = 1 WHERE id = ?", (result_id,))
        ctx.applied += 1
        return True
    return False


def _stage_insert_experiment(ctx):
    """Section 1: initialize lineage vars, auto-classify/normalize/gate the
    domain, auto-classify experiment_type, finalize tags and
    confidence_change, and upsert the experiments row; then 1a-bis
    benchmark propagation and the low-quality log line.
    """
    conn = ctx.conn
    exp_id = ctx.exp_id
    task_id = ctx.task_id
    domain = ctx.domain
    hyp_to_store = ctx.hyp_to_store
    result_text = ctx.result_text
    experiment_type = ctx.experiment_type
    tags = ctx.tags
    verdict = ctx.verdict
    confidence = ctx.confidence
    wr_model = ctx.wr_model
    created_unix = ctx.created_unix
    pred_dir_json = ctx.pred_dir_json
    obs_dir_json = ctx.obs_dir_json
    design_vec_json = ctx.design_vec_json
    quality_score = ctx.quality_score
    quality_tags = ctx.quality_tags
    mechanism_type = ctx.mechanism_type
    wr_verdict_basis = ctx.wr_verdict_basis

    # Initialize lineage vars BEFORE the experiments-write try so they
    # are bound even if the INSERT itself fails on the first reference
    # (parent_benchmark_id is used in the INSERT tuple below, then
    # checked at line ~618 — must exist on entry to the try).
    parent_curiosity_id = None
    parent_benchmark_id = None
    parent_known_answer = None
    _parent_depth = 0  # parent curiosity evidence_depth, for deep-lineage exemptions

    try:
        # Auto-classify domain if empty, "general", or one of the
        # known mislabel-magnet buckets. Prefer the EMBEDDING
        # classifier (semantic, best-match, thresholded); fall back
        # to keyword matching only if the embed server is down.
        # (The old keyword classifier used greedy first-match
        # substring matching and swept thousands of unrelated
        # experiments into calibration/bias/lexical — see backfill.)
        MISLABEL_MAGNETS = {"calibration", "bias", "lexical"}
        JUNK_DOMAINS = {"uncategorized", "unclassified_pending",
                        "split_per_experiment", "general", "",
                        "unknown"}
        if (not domain or domain in JUNK_DOMAINS
                or domain in MISLABEL_MAGNETS):
            text_for_class = f"{hyp_to_store} {result_text}"
            auto_domain = None
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from embedding_domain_classifier import classify_embedding
                res = classify_embedding(text_for_class)
                cand, score = res if isinstance(res, tuple) else (res, 1.0)
                if cand and score and score >= 0.40:
                    auto_domain = cand
            except Exception:
                pass
            if not auto_domain:
                try:
                    from auto_classify_uncategorized import classify_by_keywords
                    auto_domain = classify_by_keywords(text_for_class)
                except Exception:
                    pass
            if auto_domain:
                domain = auto_domain

        # Normalize domain to canonical form
        domain = normalize_domain(domain)

        # Domain creation gate: prevent fragmentation by
        # redirecting new/small domains to canonical parents.
        if domain:
            original_domain = domain
            try:
                from domain_creation_gate import resolve_domain, is_canonical
                if not is_canonical(domain):
                    resolved, reason, conf = resolve_domain(domain, text=hyp_to_store)
                    if resolved != domain and reason != "provisional":
                        domain = resolved
            except Exception as pass_err:
                pass  # gate is advisory — don't block results

            # Sync worker_results.domain to the gated value so
            # malformed/duplicate domains don't persist there and
            # leak into the topology builder (which reads
            # worker_results.domain directly).  Without this, the
            # gate only fixes experiments.domain while the raw
            # worker domain survives in worker_results forever.
            if domain != original_domain:
                try:
                    conn.execute(
                        "UPDATE worker_results SET domain = ? "
                        "WHERE experiment_id = ? AND domain = ?",
                        (domain, exp_id, original_domain)
                    )
                except Exception:
                    pass  # best-effort — don't block the apply

        # Final junk-domain guard: if the domain is still a junk
        # sentinel after all classification attempts, try the
        # embedding classifier one last time.  This prevents
        # 'uncategorized'/'unclassified_pending' from accumulating
        # in the topology as ghost nodes.
        if domain in ("uncategorized", "unclassified_pending",
                      "split_per_experiment", ""):
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from embedding_domain_classifier import classify_embedding as _ce
                _r = _ce(f"{hyp_to_store} {result_text}"[:2000])
                _cand, _score = _r if isinstance(_r, tuple) else (_r, 1.0)
                if _cand and _cand not in ("uncategorized",
                        "unclassified_pending", "split_per_experiment"):
                    domain = _cand
            except Exception:
                pass

        # Auto-classify experiment_type if the worker omitted it.
        # Mirrors the domain fallback above. Without this, genuine
        # experiments land with NULL type and vanish from typed
        # dashboards/reports (transfer always sets ANALOGICAL).
        if not experiment_type:
            try:
                from classify_experiment_type import classify_experiment_type as _cet
                experiment_type = _cet(
                    text=f"{hyp_to_store} {result_text}",
                    tags=tags,
                    title=hyp_to_store,
                )
            except Exception:
                experiment_type = "MECHANISTIC"  # safe documented default

        # Auto-add IMPLEMENTED tag to BUILD findings
        # This closes the build loop — workers tag BUILD but not IMPLEMENTED
        if tags and "BUILD" in tags and "IMPLEMENTED" not in tags:
            tags.append("IMPLEMENTED")

        # ROOT-CAUSE FIX (2026-06-08): guarantee a verdict tag so an
        # experiment with a known verdict is never left fully untagged,
        # even if the worker supplied no parseable tags. Belt-and-suspenders
        # alongside the _parse_tags() fix above.
        _vtag = _VERDICT_TO_TAG.get(verdict) if verdict else None
        if _vtag and _vtag not in tags:
            tags.insert(0, _vtag)

        # ── CONFIDENCE_CHANGE FIX v2 (2026-06-20) ───────────────────
        # The June 19 fix stored posterior_delta, but Beta(1,1)
        # produces ±0.1667 for EVERY first-evidence experiment —
        # 47% of all rows had the same two values. The dashboard
        # showed ±0.17 for nearly half of everything.
        #
        # v2: Sign-corrected raw worker confidence. Supported=+conf,
        # refuted=-conf. Varies per experiment and is honest about
        # the worker's actual reported certainty.
        raw_conf = float(confidence) if confidence else 0.0
        if verdict in ('REFUTED', 'PARTIALLY REFUTED', 'REFUTED_SETUP'):
            confidence_change = -abs(raw_conf)
        else:
            confidence_change = abs(raw_conf)

        conn.execute(
            """INSERT OR REPLACE INTO experiments
               (id, hypothesis, result, status, tags, domain, model,
                created_at, completed_at, predicted_direction,
                observed_direction, design_vector, quality_score, quality_tags,
                experiment_type, mechanism_type, confidence_change, benchmark_id,
                verdict_basis, kanban_task_id)
               VALUES (?, ?, ?, 'completed', ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (exp_id, hyp_to_store, result_text,
             json.dumps(tags), domain or "", wr_model or "xiaomi/mimo-v2.5",
             created_unix, created_unix,
             pred_dir_json, obs_dir_json, design_vec_json,
             quality_score, json.dumps(quality_tags),
             experiment_type, mechanism_type,
             confidence_change, parent_benchmark_id,
             wr_verdict_basis, task_id)
        )
    except Exception as e:
        print(f"  WARN: experiments insert failed for {exp_id}: {e}")

    # 1a-bis: Propagate benchmark_id onto worker_results (was NULL before)
    # parent_benchmark_id was fetched above (line ~744) — copy through.
    if parent_benchmark_id:
        try:
            conn.execute(
                "UPDATE worker_results SET benchmark_id = ? "
                "WHERE experiment_id = ? AND (benchmark_id IS NULL OR benchmark_id = '')",
                (parent_benchmark_id, exp_id)
            )
        except Exception:
            pass

    # Log low-quality results
    if quality_score < 40:
        print(f"  LOW QUALITY [{quality_score}]: {exp_id} — {','.join(quality_tags[:3])}")

    ctx.domain = domain
    ctx.experiment_type = experiment_type
    ctx.parent_curiosity_id = parent_curiosity_id
    ctx.parent_benchmark_id = parent_benchmark_id
    ctx.parent_known_answer = parent_known_answer
    ctx.parent_depth = _parent_depth


def _stage_classify_refutation(ctx):
    """1b. Classify refutation type onto the experiments row."""
    conn = ctx.conn
    exp_id = ctx.exp_id

    # 1b. Classify refutation type
    try:
        from classify_refutations import classify_single
        ref_type = classify_single(conn, exp_id)
        if ref_type:
            conn.execute(
                "UPDATE experiments SET refutation_type = ? WHERE id = ?",
                (ref_type, exp_id)
            )
    except Exception as e:
        print(f"  WARN: refutation classification failed for {exp_id}: {e}")


def _stage_attach_claim(ctx):
    """1c. Attach evidence to the knowledge claim."""
    conn = ctx.conn
    exp_id = ctx.exp_id

    # 1c. Attach evidence to knowledge claim
    try:
        from claim_lifecycle import attach_experiment
        claim_id, claim_status, posterior = attach_experiment(conn, exp_id)
        if claim_status in ('CONTESTED', 'RETIRED'):
            print(f"  CLAIM {claim_status} [{posterior:.3f}]: {exp_id} — claim #{claim_id}")
        # Also check the new claim_status column for DISPUTED claims
        if claim_id:
            new_status = conn.execute(
                "SELECT claim_status FROM knowledge_claims WHERE id = ?",
                (claim_id,)
            ).fetchone()
            if new_status and new_status[0] == 'DISPUTED':
                print(f"  DISPUTED [old={claim_status}]: {exp_id} — claim #{claim_id} (contradictions exist)")
    except Exception as e:
        print(f"  WARN: claim attachment failed for {exp_id}: {e}")


def _stage_finalize_manifest(ctx):
    """1d. Freeze the artifact manifest for gate-passers."""
    exp_id = ctx.exp_id
    task_id = ctx.task_id
    verdict = ctx.verdict
    wr_verdict_basis = ctx.wr_verdict_basis
    confidence = ctx.confidence
    quality_score = ctx.quality_score
    quality_tags = ctx.quality_tags
    domain = ctx.domain
    hyp_to_store = ctx.hyp_to_store
    key_finding = ctx.key_finding
    worker_id = ctx.worker_id
    wr_model = ctx.wr_model

    # 1d. Freeze artifact manifest for gate-passers (2026-07-01).
    # Persists the intake verdict next to the files preserved at
    # result-write time (artifact_preserve.py), so claims that
    # later promote stay auditable after workspace teardown.
    try:
        from artifact_preserve import finalize_manifest
        finalize_manifest(exp_id, task_id, {
            "verdict": verdict,
            "verdict_basis": wr_verdict_basis,
            "confidence": float(confidence) if confidence is not None else None,
            "quality_score": quality_score,
            "quality_tags": quality_tags,
            "domain": domain,
            "hypothesis": hyp_to_store,
            "key_finding": key_finding,
            "worker_id": worker_id,
            "model": wr_model,
        })
    except Exception:
        pass  # manifest is advisory, never blocking


def _stage_adversarial_routing(ctx):
    """1e. Adversarial replication outcome routing (ATTACK_OUTCOME tokens,
    boundary-mapping curiosities, claim_scopes, DISPUTED updates).
    """
    conn = ctx.conn
    exp_id = ctx.exp_id
    task_id = ctx.task_id
    result_id = ctx.result_id
    key_finding = ctx.key_finding
    verdict = ctx.verdict
    now_unix = ctx.now_unix

    # 1e. Adversarial replication outcome routing (2026-07-01).
    # Attacks enqueued by adversarial_replication_enqueuer.py carry
    # an ADVERSARIAL_REPLICATION_FOR_CLAIM marker. survived feeds
    # the ESTABLISHED gate (maturity.py established_break_survivals);
    # refuted disputes the claim (same lever as the detector).
    try:
        _adv_claim = _extract_adv_claim_id(task_id, key_finding)
        if _adv_claim:
            # Explicit outcome token first (the card demands it).
            # BROKEN disputes; NARROWED is neutral (no dispute, no
            # survival credit — claim stays attack-eligible on its
            # narrowed core); SURVIVED counts toward the
            # ESTABLISHED break-survival gate.
            # Longest token first: SURVIVED_WITHIN_SCOPE (scoped
            # re-attack cards only) is survival credit for the
            # claim AS SCOPED — the convergence outcome that stops
            # incidental limits pulling every verdict to NARROWED
            # and orbiting the narrow->map->re-attack cycle.
            _adv_within_scope = False
            _adv_tok = re.search(
                r'ATTACK_OUTCOME:\s*(BROKEN|NARROWED|'
                r'SURVIVED_WITHIN_SCOPE|SURVIVED)',
                key_finding or '', re.IGNORECASE)
            if _adv_tok:
                _tok = _adv_tok.group(1).upper()
                _adv_within_scope = (_tok == 'SURVIVED_WITHIN_SCOPE')
                _adv_status = {'BROKEN': 'refuted',
                               'NARROWED': 'narrowed',
                               'SURVIVED': 'survived',
                               'SURVIVED_WITHIN_SCOPE': 'survived'}[_tok]
            elif verdict == 'PARTIALLY REFUTED':
                _adv_status = 'narrowed'  # partial break = scope info
            elif verdict == 'REFUTED':
                _adv_status = 'refuted'
            elif verdict == 'SUPPORTED':
                _adv_status = 'survived'
            elif verdict == 'REFUTED_SETUP':
                _adv_status = 'expired'  # attack never ran; claim eligible again
            else:
                _adv_status = None
            if _adv_status:
                _adv_cur = conn.execute(
                    "UPDATE adversarial_replications SET status = ?, "
                    "resolved_at = ?, experiment_id = ?, notes = ? "
                    "WHERE claim_id = ? AND status = 'pending'",
                    (_adv_status, now_unix, exp_id,
                     (key_finding or '')[:200], _adv_claim))
                if _adv_cur.rowcount:
                    print(f"  [ADVERSARIAL] claim {_adv_claim}: "
                          f"{_adv_status} by {exp_id}")
                    if _adv_status == 'narrowed':
                        # Boundary capture: a NARROWED outcome means
                        # the attack FOUND the claim's regime boundary
                        # — knowledge that otherwise lives only in
                        # this finding's prose. Spawn one boundary-
                        # mapping curiosity per claim so a worker
                        # pins down the surviving core and its
                        # limits. source_result_id links it to this
                        # result (experiment-backed depth).
                        # Dedup on ACTIVE only. Any-status-ever was a
                        # lockout: a boundary curiosity that age-retired
                        # unrun permanently blocked respawn, so the claim
                        # cycled attack->NARROWED forever with no boundary
                        # path (same poisoned-dedup lesson as the retest
                        # lane). A RESOLVED boundary doesn't block either:
                        # with claim scoping, a NEW narrowed outcome after
                        # the old boundary was mapped means a NEW boundary
                        # was found — it deserves its own mapping question.
                        _bnd_dup = conn.execute(
                            "SELECT 1 FROM curiosities WHERE "
                            "provenance = 'narrowed_boundary' AND "
                            "status = 'active' AND "
                            "text LIKE ? LIMIT 1",
                            (f"[BOUNDARY] Claim {_adv_claim} %",)).fetchone()
                        if not _bnd_dup:
                            conn.execute(
                                "INSERT INTO curiosities "
                                "(text, priority, status, source_experiment, "
                                " source_result_id, provenance, created_at) "
                                "VALUES (?, 5, 'active', ?, ?, 'narrowed_boundary', ?)",
                                (f"[BOUNDARY] Claim {_adv_claim} survived attack "
                                 f"only in a narrowed regime — map the boundary: "
                                 f"state the surviving core, the regime where it "
                                 f"holds, and where it fails. ATTACK FINDING: "
                                 f"{(key_finding or '')[:400]}",
                                 exp_id, result_id, now_unix))
                            print(f"  [BOUNDARY] spawned boundary-mapping "
                                  f"curiosity for claim {_adv_claim}")
                    if _adv_status == 'survived' and _adv_within_scope:
                        # SURVIVED_WITHIN_SCOPE: survival credit was
                        # written above (status='survived' satisfies
                        # the ESTABLISHED break-survival gate — the
                        # claim AS SCOPED withstood the attack, and
                        # claim_summary headlines scoped corrections).
                        # The incidental limit the attacker stated is
                        # appended to claim_scopes so it isn't lost,
                        # and NO boundary question spawns — the
                        # refinement loop terminates here.
                        try:
                            conn.execute(
                                "CREATE TABLE IF NOT EXISTS claim_scopes ("
                                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                                " claim_id INTEGER NOT NULL,"
                                " scope_text TEXT NOT NULL,"
                                " source_experiment TEXT,"
                                " source_curiosity_id INTEGER,"
                                " created_at REAL,"
                                " UNIQUE(claim_id, source_curiosity_id))")
                            conn.execute(
                                "INSERT INTO claim_scopes "
                                "(claim_id, scope_text, source_experiment,"
                                " source_curiosity_id, created_at) "
                                "VALUES (?, ?, ?, NULL, ?)",
                                (_adv_claim,
                                 (key_finding or '')[:1200],
                                 exp_id, now_unix))
                            print(f"  [SCOPE] within-scope survival: "
                                  f"incidental limit appended for "
                                  f"claim {_adv_claim}")
                        except Exception as _se:
                            print(f"  WARN: scope append failed: {_se}")
                    if _adv_status == 'refuted':
                        conn.execute(
                            "UPDATE knowledge_claims SET claim_status = 'DISPUTED', "
                            "contradiction_count = COALESCE(contradiction_count, 0) + 1, "
                            "last_updated_at = ? WHERE id = ?",
                            (now_unix, _adv_claim))
                        print(f"  [ADVERSARIAL] claim {_adv_claim} DISPUTED "
                              f"(attack succeeded)")
    except Exception as e:
        print(f"  WARN: adversarial routing failed for {exp_id}: {e}")


def _stage_dispute_arbitration(ctx):
    """1f. Dispute arbitration outcome routing."""
    conn = ctx.conn
    exp_id = ctx.exp_id
    task_id = ctx.task_id
    key_finding = ctx.key_finding
    now_unix = ctx.now_unix

    # 1f. Dispute arbitration outcome routing (see
    # dispute_arbitration_enqueuer.py). A decisive experiment
    # settles which side of a stored contradiction survives:
    # losing evidence is retracted, the dispute's contradiction
    # bump is cleared, and the maturity recompute lifts DISPUTED
    # when no contradictions remain.
    try:
        _arb_claim = _extract_marker_id(
            task_id, key_finding,
            r"DISPUTE_ARBITRATION_FOR_CLAIM:\s*(\d+)")
        if _arb_claim:
            _vm2 = re.search(
                r"ARBITRATION_VERDICT:\s*(A_CORRECT|B_CORRECT|"
                r"BOTH_WRONG|REGIME_SPLIT)",
                key_finding or "", re.IGNORECASE)
            _arb_token = _vm2.group(1).upper() if _vm2 else None
            from dispute_arbitration_enqueuer import resolve_arbitration
            _arb_status = resolve_arbitration(
                conn, _arb_claim, _arb_token, exp_id,
                key_finding, now=now_unix)
            if _arb_status:
                print(f"  [ARBITRATION] claim {_arb_claim}: "
                      f"{_arb_token or 'no-verdict'} -> {_arb_status} "
                      f"by {exp_id}")
    except Exception as e:
        print(f"  WARN: arbitration routing failed for {exp_id}: {e}")


def _stage_retest_credit(ctx):
    """1g. Retest credit: record the replication outcome and resolve the
    retest curiosity at the moment of credit.
    """
    conn = ctx.conn
    exp_id = ctx.exp_id
    task_id = ctx.task_id
    key_finding = ctx.key_finding
    confidence = ctx.confidence
    verdict = ctx.verdict
    now_unix = ctx.now_unix

    # 1g. Retest credit (replaces the lost validator_selector.py
    # writer — replication_results had no writer since 2026-06-21,
    # so completed [CANDIDATE-RETEST] experiments earned no
    # n_independent_retests credit and 1,183 claims froze at the
    # retest gate). When this result's parent curiosity is a
    # retest injection, record the replication outcome; the
    # detector cycle recomputes the counters from the table.
    try:
        _rt_cur_id = _extract_curiosity_id_from_task(task_id)
        if _rt_cur_id and verdict and verdict != 'REFUTED_SETUP':
            _rt_cur = conn.execute(
                "SELECT provenance, source_experiment, created_at "
                "FROM curiosities WHERE id = ?", (_rt_cur_id,)).fetchone()
            if (_rt_cur and _rt_cur[0] in ('candidate_retest', 'retest_gate')
                    and _rt_cur[1] and _rt_cur[1] != exp_id):
                # Dedup on the SOURCE experiment — the table's
                # UNIQUE constraint is original_experiment_id (one
                # credit per source). Probing validation_experiment_id
                # let every REPEAT retest reach the INSERT, throw
                # UNIQUE, and abort this try before the curiosity
                # resolve below — so the curiosity stayed active and
                # re-dispatched in a loop (50% of the retest lane's
                # 24h capacity measured on duplicates).
                _rt_dup = conn.execute(
                    "SELECT 1 FROM replication_results "
                    "WHERE original_experiment_id = ? LIMIT 1",
                    (_rt_cur[1],)).fetchone()
                if not _rt_dup:
                    _rt_orig = conn.execute(
                        "SELECT result, domain FROM experiments WHERE id = ?",
                        (_rt_cur[1],)).fetchone()
                    _rt_status = ('replicated' if verdict == 'SUPPORTED'
                                  else 'disagreed')
                    conn.execute("""
                        INSERT OR IGNORE INTO replication_results
                        (original_experiment_id, original_finding,
                         original_domain, validation_task_id,
                         validation_experiment_id, validation_finding,
                         validation_confidence,
                         validation_hypothesis_supported,
                         replication_status, selected_at, validated_at,
                         selection_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                'candidate_retest_intake')""",
                        (_rt_cur[1],
                         (_rt_orig[0] if _rt_orig else None),
                         (_rt_orig[1] if _rt_orig else None),
                         task_id, exp_id, key_finding,
                         float(confidence) if confidence is not None else None,
                         1 if verdict == 'SUPPORTED' else 0,
                         _rt_status, _rt_cur[2], now_unix))
                    print(f"  [RETEST] {exp_id} -> {_rt_status} "
                          f"(original {_rt_cur[1]})")
                # Resolve the retest curiosity at the moment of
                # credit. The Jaccard resolver is source-guarded
                # and may never text-match this experiment on its
                # own; without this the curiosity stays active and
                # gets re-dispatched.
                conn.execute(
                    "UPDATE curiosities SET status='resolved', "
                    "resolved_by_experiment=?, resolved_at=? "
                    "WHERE id=? AND status='active'",
                    (exp_id, now_unix, _rt_cur_id))
    except Exception as e:
        print(f"  WARN: retest credit failed for {exp_id}: {e}")


def _stage_update_state_counters(ctx):
    """Section 2 counters: experiments_completed_list and the count fan-out
    (only called when state is truthy).
    """
    state = ctx.state
    exp_id = ctx.exp_id

    # experiments_completed_list is a list of ID strings
    ecl = state.get("experiments_completed_list", [])
    if exp_id not in ecl:
        ecl.append(exp_id)
        state["experiments_completed_list"] = ecl

    # Update all the count fields that reference completed experiments
    count = len(ecl)
    state["experiments_completed_list_len"] = count
    state["completed"] = count
    state["experiments_completed"] = count
    state["total_experiments_completed"] = count
    state["total_experiments"] = count
    state["experiments_completed_count"] = count
    state["count"] = count
    state["completed_count"] = count
    state["experiments"] = count  # this field is an int, not a dict

    # Update metrics
    metrics = state.get("metrics", {})
    metrics["experiments_completed"] = count
    metrics["experiments_conducted"] = count
    metrics["experiments_completed_count"] = count
    state["metrics"] = metrics

    # Update counters
    counters = state.get("counters", {})
    counters["experiments_completed"] = count
    counters["experiments_total"] = count
    counters["total_experiments"] = count
    counters["experiments_completed_count"] = count
    counters["count"] = count
    state["counters"] = counters


def _stage_resolve_parent_curiosity(ctx):
    """Section 2 lineage: resolve the parent curiosity (Methods 1-4),
    boundary-lane closure + claim scoping, benchmark inheritance, and
    mark the parent answered.
    """
    conn = ctx.conn
    exp_id = ctx.exp_id
    task_id = ctx.task_id
    key_finding = ctx.key_finding
    hyp_to_store = ctx.hyp_to_store
    now_unix = ctx.now_unix
    parent_curiosity_id = ctx.parent_curiosity_id
    parent_benchmark_id = ctx.parent_benchmark_id
    parent_known_answer = ctx.parent_known_answer
    _parent_depth = ctx.parent_depth

    # Add curiosities to SQLite curiosities table (canonical store)
    # Dedup sets (existing_curiosities, completed_hyps) pre-loaded before loop

    # Look up parent curiosity: try task body first (exact), then text match (fallback)
    # NOTE: parent_curiosity_id / parent_benchmark_id / parent_known_answer
    # are initialized above (before the experiments-write try) so this
    # block only needs to populate them.
    try:
        # Method 1: Read CURIOSITY_ID from the kanban task body (exact, no guessing)
        parent_curiosity_id = _extract_curiosity_id_from_task(task_id)

        # Boundary-lane closure: narrowed_boundary curiosities
        # are resolved HERE, on completion of their own task —
        # their quote-heavy text (they embed the attack
        # finding) makes the Jaccard resolver unreliable for
        # them, the same lesson as the retest lane.
        if parent_curiosity_id:
            try:
                _bcur = conn.execute(
                    "SELECT provenance, text FROM curiosities "
                    "WHERE id = ? AND status = 'active'",
                    (parent_curiosity_id,)).fetchone()
                if _bcur and _bcur[0] == 'narrowed_boundary':
                    conn.execute(
                        "UPDATE curiosities SET status='resolved', "
                        "resolved_by_experiment=?, resolved_at=? "
                        "WHERE id=? AND status='active'",
                        (exp_id, now_unix, parent_curiosity_id))
                    print(f"  [BOUNDARY] resolved curiosity "
                          f"{parent_curiosity_id} by {exp_id}")
                    # CLAIM SCOPING: the mapped regime otherwise
                    # dies in this finding's prose — the claim
                    # keeps its universal statement and the next
                    # attacker rediscovers the same out-of-regime
                    # hole (claims re-NARROWED up to 21x). Persist
                    # the boundary finding to the claim_scopes
                    # ledger; adversarial_replication_enqueuer
                    # bakes the latest scope into re-attack cards
                    # ("attack WITHIN the mapped regime"), which
                    # is what makes SURVIVED reachable. Dedicated
                    # ledger, NOT claim_evidence (orphan sweep;
                    # and a scope is context, not support).
                    _bm = re.match(
                        r"\[BOUNDARY\] Claim (\d+)\b",
                        _bcur[1] or "")
                    _scope_txt = (key_finding or "").strip()
                    if _bm and _scope_txt:
                        conn.execute(
                            "CREATE TABLE IF NOT EXISTS claim_scopes ("
                            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                            " claim_id INTEGER NOT NULL,"
                            " scope_text TEXT NOT NULL,"
                            " source_experiment TEXT,"
                            " source_curiosity_id INTEGER,"
                            " created_at REAL,"
                            " UNIQUE(claim_id, source_curiosity_id))")
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS "
                            "idx_claim_scopes_claim "
                            "ON claim_scopes(claim_id)")
                        conn.execute(
                            "INSERT OR IGNORE INTO claim_scopes "
                            "(claim_id, scope_text, source_experiment,"
                            " source_curiosity_id, created_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (int(_bm.group(1)), _scope_txt[:1200],
                             exp_id, parent_curiosity_id, now_unix))
                        print(f"  [SCOPE] recorded mapped regime for "
                              f"claim {_bm.group(1)} from {exp_id}")
            except Exception as _be:
                print(f"  WARN: boundary resolve failed: {_be}")

        # Method 2: Fallback to text matching if no CURIOSITY_ID in body
        if not parent_curiosity_id:
            hyp_to_match = hyp_to_store or key_finding or ""
            if hyp_to_match and len(hyp_to_match) > 10:
                prow = conn.execute(
                    "SELECT id FROM curiosities WHERE text LIKE ? ORDER BY id DESC LIMIT 1",
                    (f"%{hyp_to_match[:80]}%",)
                ).fetchone()
                if prow:
                    parent_curiosity_id = prow[0]

        # Method 3: Fallback to source_experiment lookup
        # If the experiment was linked to a curiosity at creation time,
        # find it via the source_experiment field. This catches tasks
        # created without CURIOSITY_ID (synthesis, cross-domain, etc.)
        if not parent_curiosity_id and exp_id:
            try:
                prow = conn.execute(
                    "SELECT id FROM curiosities WHERE source_experiment = ? LIMIT 1",
                    (exp_id,)
                ).fetchone()
                if prow:
                    parent_curiosity_id = prow[0]
            except Exception:
                pass

        # Method 4: Create root curiosity if none found
        # When tasks are created without CURIOSITY_ID (synthesis,
        # cross-domain injection, etc.), no parent exists. Create a
        # root curiosity so follow-up questions have a lineage parent.
        if not parent_curiosity_id and exp_id:
            try:
                _root_text = hyp_to_store or key_finding or f"Experiment {exp_id}"
                if _root_text and len(_root_text) > 10:
                    conn.execute(
                        "INSERT INTO curiosities (text, priority, status, "
                        "source_experiment, created_at) VALUES (?, 3, 'active', ?, ?)",
                        (_root_text[:500], exp_id, now_unix)
                    )
                    parent_curiosity_id = conn.execute(
                        "SELECT id FROM curiosities WHERE source_experiment = ? LIMIT 1",
                        (exp_id,)
                    ).fetchone()[0]
                    print(f"  [LINEAGE] Created root curiosity #{parent_curiosity_id} for {exp_id}")
            except Exception:
                pass

        # Benchmark tag propagation: if parent has benchmark_id, inherit it.
        # Also capture parent depth for deep-lineage exemptions below.
        _parent_depth = 0
        if parent_curiosity_id:
            bprow = conn.execute(
                "SELECT benchmark_id, known_answer, COALESCE(evidence_depth, 0) FROM curiosities WHERE id = ?",
                (parent_curiosity_id,)
            ).fetchone()
            if bprow and bprow[0]:
                parent_benchmark_id = bprow[0]
                parent_known_answer = bprow[1]
            if bprow:
                try:
                    _parent_depth = int(bprow[2] or 0)
                except (ValueError, TypeError):
                    _parent_depth = 0
    except Exception:
        pass

    # Update parent curiosity with experiment link so it's marked
    # as answered. This closes the loop: BENCH3 question → worker
    # experiment → result flows back to calibration.
    if parent_curiosity_id and exp_id:
        try:
            conn.execute(
                "UPDATE curiosities SET source_experiment=?, resolved_by_experiment=?, "
                "status='resolved', resolved_at=? WHERE id=? AND source_experiment IS NULL",
                (exp_id, exp_id, now_unix, parent_curiosity_id)
            )
        except Exception:
            pass

    ctx.parent_curiosity_id = parent_curiosity_id
    ctx.parent_benchmark_id = parent_benchmark_id
    ctx.parent_known_answer = parent_known_answer
    ctx.parent_depth = _parent_depth


def _stage_check_generation_throttle(ctx):
    """Section 2 throttle probe: reset the inserted counter, look up the
    parent source, and ask the generation throttle.
    """
    conn = ctx.conn
    exp_id = ctx.exp_id

    inserted = 0

    # Generation throttle: cap new curiosities per hour
    # Benchmark questions are exempt
    # Source-aware: refutation_boost descendants get their own quota
    _throttle_active = False
    _parent_source = None  # init before try to prevent UnboundLocalError
    try:
        from generation_throttle import should_throttle
        # Keep the just-imported binding on ctx: the branching stages
        # below call it exactly like the original shared function-local
        # name (import failure stays fail-open).
        ctx.should_throttle = should_throttle
        # Check if parent experiment is refutation-boost
        _parent_source = conn.execute(
            "SELECT source_experiment FROM curiosities WHERE resolved_by_experiment = ? LIMIT 1",
            (exp_id,)
        ).fetchone()
        _source = "refutation_boost" if _parent_source and _parent_source[0] == "refutation_boost" else None
        _throttle_active = should_throttle(conn, source=_source)
        if _throttle_active:
            print(f"  [THROTTLE] Generation cap reached — skipping new curiosity creation (source={_source})")
    except Exception:
        pass

    ctx.inserted = inserted
    ctx.throttle_active = _throttle_active
    ctx.parent_source = _parent_source


def _stage_insert_queue_additions(ctx):
    """Section 2 queue intake: word-count/dedup screens, the weighted-support
    evidence gate, throttle, benchmark bypass, curiosity inserts, and
    explicit [TRANSFER] tracking.
    """
    conn = ctx.conn
    exp_id = ctx.exp_id
    result_id = ctx.result_id
    domain = ctx.domain
    hyp_to_store = ctx.hyp_to_store
    now_unix = ctx.now_unix
    queue_additions = ctx.queue_additions
    existing_curiosities = ctx.existing_curiosities
    completed_hyps = ctx.completed_hyps
    parent_curiosity_id = ctx.parent_curiosity_id
    parent_benchmark_id = ctx.parent_benchmark_id
    _parent_depth = ctx.parent_depth
    _throttle_active = ctx.throttle_active
    _parent_source = ctx.parent_source
    inserted = ctx.inserted

    for item_text in queue_additions:
        if item_text and len(item_text) > 10:
            item_words = set(re.findall(r'\w{4,}', item_text.lower()))
            if len(item_words) < 3:
                print(f"  [QA-SKIP] too few words: '{item_text[:60]}'")
                continue
            is_dup = False

            # Skip O(N*M) dedup when sets are too large (causes 300s timeouts)
            # The dedup is nice-to-have; blocking experiment creation for it
            # is not worth it. Curiosity_queue GC handles duplicates separately.
            MAX_DEDUP_SET_SIZE = 10000
            if len(existing_curiosities) <= MAX_DEDUP_SET_SIZE:
                for existing_text in existing_curiosities:
                    existing_words = set(re.findall(r'\w{4,}', existing_text))
                    if existing_words:
                        overlap = len(item_words & existing_words) / max(len(item_words | existing_words), 1)
                        if overlap > 0.55:
                            is_dup = True
                            break
            else:
                is_dup = False  # skip dedup, accept potential duplicates

            if not is_dup and len(completed_hyps) <= MAX_DEDUP_SET_SIZE:
                for hyp in completed_hyps:
                    hyp_words = set(re.findall(r'\w{4,}', hyp))
                    if hyp_words:
                        overlap = len(item_words & hyp_words) / max(len(item_words | hyp_words), 1)
                        if overlap > 0.55:
                            is_dup = True
                            break

            if is_dup:
                print(f"  [QA-DEDUP] duplicate skipped: '{item_text[:60]}'")
                continue

            # Phase 4.1: Evidence accumulation gate
            # Only generate questions from findings that have been
            # supported by 2+ experiments (REPLICATED status)
            # Single-experiment findings are still recorded but
            # don't immediately spawn new questions
            _skip_question = False
            _gate_reason = ""
            try:
                # FIX (2026-06-19): Use weighted_support_count instead of support_count.
                # support_count is a stale denormalized field that drifts from reality
                # (claim #18756: support_count=1009 but 7 real evidence rows, wsc=0.0).
                # This was causing 99.5% of depth-0 curiosities to die — the gate was
                # blocking based on a broken field. wsc accounts for artifact_status
                # (HISTORICAL_UNKNOWN zeroes out). Threshold 1.0 = at least one verified,
                # consistent support with real artifacts.
                _claim = conn.execute(
                    "SELECT weighted_support_count FROM knowledge_claims WHERE hypothesis_text = ?",
                    (hyp_to_store,)
                ).fetchone()
                if _claim and _claim[0] is not None and _claim[0] < 1.0:
                    # Claim has < 1.0 weighted support — hold off on generating questions
                    # unless this is a [TRANSFER] question (transfers are boundary-seeking)
                    # FIX (2026-06-23): also exempt DEEP lineages (parent depth >= 8).
                    # A fresh deep finding always has wsc=0.0 (it's the sole support for
                    # its own brand-new claim), so the gate blocked every non-TRANSFER
                    # deepening follow-up — capping the depth-climb. Deep lineages are
                    # rare and valuable; let them deepen regardless of accumulated support.
                    _is_deep = _parent_depth >= 8
                    if "[TRANSFER" not in item_text.upper() and not _is_deep:
                        _skip_question = True
                        _gate_reason = f"wsc={_claim[0]:.2f}<1.0, non-TRANSFER, depth={_parent_depth}"
            except Exception:
                pass

            if _skip_question and not parent_benchmark_id:
                print(f"  [QA-GATE] evidence gate blocked: {_gate_reason} — '{item_text[:60]}'")
                continue

            if not parent_benchmark_id and _throttle_active and _parent_depth < 8:
                # FIX (2026-06-23): exempt deep lineages (parent depth >= 8) from the
                # generation throttle. The 250/hr cap is FIFO and indiscriminate — at
                # ~200 exp/hr, deep follow-ups competed with the shallow synthesis/
                # injection flood and lost, so the depth-climb stalled at d17. Deep
                # deepening follow-ups are rare (a few per cycle) and must not be dropped.
                print(f"  [QA-THROTTLE] throttle blocked: '{item_text[:60]}'")
                continue
            # Benchmark lineages bypass the evidence accumulation gate
            # — we want all children to spawn regardless of support count
            if parent_benchmark_id:
                # FIX (2026-06-19): Do NOT propagate known_answer to children.
                # Children are genuinely different questions whose truth value
                # cannot be inherited from the parent. Only propagate benchmark_id
                # for lineage tracking. Known_answer is NULL for depth-1+ —
                # they need their own external truth evaluation.
                _prov = json.dumps({"parser": "new", "origin": "benchmark", "interventions": ["I6"], "depth": 0})
                conn.execute(
                    """INSERT INTO curiosities (text, priority, status, source_experiment, created_at, parent_curiosity_id, provenance)
                   VALUES (?, 3, 'active', ?, ?, ?, ?)""",
                    (item_text, exp_id, now_unix, parent_curiosity_id, _prov)
                )
            else:
                # Determine provenance for this curiosity
                _prov_origin = "transfer" if "[TRANSFER" in item_text.upper() else "follow_up"
                _prov_interventions = ["I6"]  # parser fix always applies
                if _parent_source and _parent_source[0] == "refutation_boost":
                    _prov_interventions.extend(["I2", "I3", "I4"])  # routing + throttle fixes
                    _prov_origin = "refutation_boost_descendant"
                _prov = json.dumps({
                    "parser": "new",
                    "origin": _prov_origin,
                    "interventions": _prov_interventions,
                    "parent_source": _parent_source[0] if _parent_source else None,
                    "created_at_fix": True
                })
                conn.execute(
                    """INSERT INTO curiosities (text, priority, status, source_experiment, created_at, parent_curiosity_id, provenance)
                   VALUES (?, 3, 'active', ?, ?, ?, ?)""",
                    (item_text, exp_id, now_unix, parent_curiosity_id, _prov)
                )
            existing_curiosities.add(item_text.lower())
            inserted += 1
            print(f"  [QA-INSERT] curiosity created: '{item_text[:60]}'")

            # Transfer tracking: insert row for [TRANSFER] items
            # Use main conn directly — opening a separate connection
            # causes "database is locked" (two writers on same DB)
            if "[TRANSFER" in item_text.upper():
                target = parse_transfer_target(item_text) or domain
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO transfer_tracking
                           (source_result_id, source_domain, target_domain,
                            status, created_at, transfer_source)
                           VALUES (?, ?, ?, 'queued', ?, 'explicit_transfer')""",
                        (result_id, domain, target, now_unix)
                    )
                except Exception as e:
                    print(f"  transfer_tracking insert: {e}")
                # NOTE: Lineage_live detection for grandparent→parent transfers
                # was previously here (elif branch) but was redundant with the
                # Path 2 check below (lines ~851). Both performed the exact same
                # query: curiosities.source_experiment.domain WHERE c.id = parent_curiosity_id.
                # Path 2 is strictly more general (fires once per WR, not per item,
                # and doesn't depend on _skip_question). Removed 2026-06-19.

    ctx.inserted = inserted


def _stage_refutation_branching(ctx):
    """Section 2 refutation branching: synthetic boundary-exploring
    follow-ups when a refuted result produced fewer than 2 questions.
    """
    conn = ctx.conn
    exp_id = ctx.exp_id
    verdict = ctx.verdict
    hyp_to_store = ctx.hyp_to_store
    now_unix = ctx.now_unix
    existing_curiosities = ctx.existing_curiosities
    parent_curiosity_id = ctx.parent_curiosity_id
    parent_benchmark_id = ctx.parent_benchmark_id
    _parent_source = ctx.parent_source
    inserted = ctx.inserted

    # ── REFUTATION BRANCHING FIX (2026-06-19) ──────────────────
    # Problem: Refuted hypotheses produce 1.47x FEWER follow-up
    # questions than confirmed ones. This collapsed the branching
    # factor at d11+ from 0.984 to 0.392, killing deep lineages.
    #
    # Fix: When a hypothesis is REFUTED and the worker produced
    # fewer than 2 follow-up questions, generate synthetic
    # boundary-exploring questions. These explore WHY the
    # hypothesis failed, what conditions would flip the result,
    # and whether the finding transfers to adjacent domains.
    # This decouples branching from confirmation — refutation
    # becomes a branching trigger, not a dead end.
    #
    # Only fires for REFUTED verdicts (not SUPPORTED or PARTIAL).
    # Skips benchmark questions (they have their own lineage).
    # Respects the generation throttle (dedicated quota for refutation followups).
    if (verdict in ('REFUTED', 'PARTIALLY REFUTED', 'REFUTED_SETUP')
            and not parent_benchmark_id
            and inserted < 2):
        # Check refutation_followup quota separately (30 dedicated slots/hr)
        try:
            _rf_throttled = ctx.should_throttle(conn, source="refutation_followup")
        except Exception:
            _rf_throttled = False
        if _rf_throttled:
            print(f"  [RF-THROTTLE] refutation_followup quota reached — skipping")
        else:
            _refutation_followups = []

            if verdict == 'REFUTED_SETUP':
                # Setup failed — try a simpler experimental design
                _refutation_followups.append(
                    f"Can '{hyp_to_store[:80]}' be tested with a simplified experimental design using fewer variables?"
                )
            elif verdict == 'PARTIALLY REFUTED':
                # Partial refutation — mutate the variable that showed partial support
                _refutation_followups.append(
                    f"Which specific parameter in '{hyp_to_store[:80]}' showed partial support, and does strengthening it push toward full support?"
                )
                _refutation_followups.append(
                    f"[TRANSFER] Does the partially-supported component of '{hyp_to_store[:80]}' transfer to a related domain?"
                )
            else:
                # Full refutation — mutate one variable at a time instead of
                # recursively asking for mechanisms. Transfer-style mutations
                # produce 12x more children (BF=0.78 vs 0.07 for mechanism-
                # recursion and boundary-condition templates).
                #
                # Guard against recursive nesting: if hyp_to_store is itself
                # a template-generated question (flip, boundary, transfer),
                # skip generating more template questions to prevent infinite
                # self-referential chains.
                _hyp_lower_rf = hyp_to_store.lower()
                _is_template_rf = ("would flip" in _hyp_lower_rf
                                   or "specific variable" in _hyp_lower_rf
                                   or "if changed, would" in _hyp_lower_rf
                                   or "boundary conditions" in _hyp_lower_rf
                                   or "would cause" in _hyp_lower_rf
                                   or "to fail" in _hyp_lower_rf
                                   or "[transfer]" in _hyp_lower_rf)
                if not _is_template_rf:
                    _refutation_followups.append(
                        f"Which specific variable, if changed, would flip the result of '{hyp_to_store[:80]}' from refuted to supported?"
                    )
                    _refutation_followups.append(
                        f"[TRANSFER] Does the mechanism tested in '{hyp_to_store[:80]}' behave differently in a neighboring domain?"
                    )

            for rf_text in _refutation_followups:
                if len(rf_text) < 10:
                    continue
                # Light dedup against existing
                rf_words = set(re.findall(r'\w{4,}', rf_text.lower()))
                if len(rf_words) < 3:
                    continue
                is_rf_dup = False
                if len(existing_curiosities) <= 10000:
                    for existing_text in existing_curiosities:
                        existing_words = set(re.findall(r'\w{4,}', existing_text))
                        if existing_words:
                            overlap = len(rf_words & existing_words) / max(len(rf_words | existing_words), 1)
                            if overlap > 0.55:
                                is_rf_dup = True
                                break
                if is_rf_dup:
                    continue

                _rf_prov = json.dumps({
                    "parser": "new",
                    "origin": "refutation_followup",
                    "interventions": ["I6", "refutation_branching"],
                    "parent_source": _parent_source[0] if _parent_source else None,
                    "verdict": verdict,
                    "created_at_fix": True
                })
                conn.execute(
                    """INSERT INTO curiosities (text, priority, status, source_experiment, created_at, parent_curiosity_id, provenance)
                   VALUES (?, 3, 'active', ?, ?, ?, ?)""",
                    (rf_text, exp_id, now_unix, parent_curiosity_id, _rf_prov)
                )
                existing_curiosities.add(rf_text.lower())
                inserted += 1
                print(f"  [RF-INSERT] refutation followup: '{rf_text[:60]}'")

    ctx.inserted = inserted


def _stage_confirmed_branching(ctx):
    """Section 2 confirmed branching: transfer/boundary follow-ups when a
    supported result produced fewer than 2 questions.
    """
    conn = ctx.conn
    exp_id = ctx.exp_id
    verdict = ctx.verdict
    hyp_to_store = ctx.hyp_to_store
    now_unix = ctx.now_unix
    existing_curiosities = ctx.existing_curiosities
    parent_curiosity_id = ctx.parent_curiosity_id
    parent_benchmark_id = ctx.parent_benchmark_id
    _parent_source = ctx.parent_source
    inserted = ctx.inserted

    # Confirmed mechanism → transfer questions.
    # Architecture: "confirmed mechanisms → transfer questions (horizontal
    # domain crossing or vertical abstraction ascent)". When a hypothesis
    # is SUPPORTED/CONFIRMED and the worker produced fewer than 2 follow-ups,
    # generate transfer questions that test the mechanism in adjacent domains.
    # This is the confirmed-side counterpart to refutation branching.
    if (verdict in ('SUPPORTED', 'CONFIRMED')
            and not parent_benchmark_id
            and inserted < 2):
        try:
            _cf_throttled = ctx.should_throttle(conn, source=None)
        except Exception:
            _cf_throttled = False
        if not _cf_throttled:
            _confirmed_followups = []
            _confirmed_followups.append(
                f"[TRANSFER] Does the mechanism confirmed in '{hyp_to_store[:80]}' transfer to a related domain?"
            )
            # Guard against recursive nesting: if hyp_to_store is itself
            # a template-generated question, skip to prevent infinite
            # recursion. Catches boundary-conditions, flip-the-result,
            # and transfer questions that would self-nest if wrapped.
            # (e.g. "What boundary conditions would cause
            # 'What boundary conditions would cause '...' to fail?'"
            #  or  "What boundary conditions would cause
            # 'Which specific variable, if changed, would flip...' to fail?")
            _hyp_lower = hyp_to_store.lower()
            _is_template_q = ("boundary conditions" in _hyp_lower
                               or "would cause" in _hyp_lower
                               or "to fail" in _hyp_lower
                               or "would flip" in _hyp_lower
                               or "specific variable" in _hyp_lower
                               or "if changed, would" in _hyp_lower
                               or "[transfer]" in _hyp_lower)
            if not _is_template_q:
                _confirmed_followups.append(
                    f"What boundary conditions would cause '{hyp_to_store[:80]}' to fail?"
                )

            for cf_text in _confirmed_followups:
                if len(cf_text) < 10:
                    continue
                cf_words = set(re.findall(r'\w{4,}', cf_text.lower()))
                if len(cf_words) < 3:
                    continue
                is_cf_dup = False
                if len(existing_curiosities) <= 10000:
                    for existing_text in existing_curiosities:
                        existing_words = set(re.findall(r'\w{4,}', existing_text))
                        if existing_words:
                            overlap = len(cf_words & existing_words) / max(len(cf_words | existing_words), 1)
                            if overlap > 0.55:
                                is_cf_dup = True
                                break
                if is_cf_dup:
                    continue

                _cf_prov = json.dumps({
                    "parser": "new",
                    "origin": "confirmed_followup",
                    "interventions": ["I6", "confirmed_branching"],
                    "parent_source": _parent_source[0] if _parent_source else None,
                    "verdict": verdict,
                    "created_at_fix": True
                })
                conn.execute(
                    """INSERT INTO curiosities (text, priority, status, source_experiment, created_at, parent_curiosity_id, provenance)
                   VALUES (?, 3, 'active', ?, ?, ?, ?)""",
                    (cf_text, exp_id, now_unix, parent_curiosity_id, _cf_prov)
                )
                existing_curiosities.add(cf_text.lower())
                inserted += 1
                print(f"  [CF-INSERT] confirmed followup: '{cf_text[:60]}'")

    ctx.inserted = inserted


def _stage_lineage_live_transfer(ctx):
    """Lineage-based cross-domain transfer detection (child side)."""
    conn = ctx.conn
    result_id = ctx.result_id
    domain = ctx.domain
    hyp_to_store = ctx.hyp_to_store
    now_unix = ctx.now_unix
    parent_curiosity_id = ctx.parent_curiosity_id

    # FIX (2026-06-19): Lineage-based cross-domain transfer detection.
    # At this point we KNOW the current experiment's domain. We can look up
    # the parent curiosity's domain and detect cross-domain transfers.
    # This runs when processing the CHILD's worker_result — the right time,
    # because the child's domain is now known.
    #
    # FIX (2026-06-19): Skip if this experiment's hypothesis contains [TRANSFER].
    # [TRANSFER] experiments were already tracked as explicit_transfer when the
    # parent generated the curiosity. Without this guard, the same transfer gets
    # logged twice: once as explicit_transfer (at parent processing time) and
    # once as lineage_live (here, at child processing time).
    if parent_curiosity_id and domain and "[TRANSFER" not in (hyp_to_store or "").upper():
        try:
            parent_row = conn.execute(
                """SELECT e.domain FROM curiosities c
                   JOIN experiments e ON e.id = c.source_experiment
                   WHERE c.id = ?""",
                (parent_curiosity_id,)
            ).fetchone()
            if parent_row and parent_row[0]:
                parent_dom = normalize_domain(parent_row[0])
                child_dom = normalize_domain(domain)
                if parent_dom != child_dom:
                    conn.execute(
                        """INSERT OR IGNORE INTO transfer_tracking
                           (source_result_id, source_domain, target_domain,
                            status, created_at, transfer_source)
                           VALUES (?, ?, ?, 'queued', ?, 'lineage_live')""",
                        (result_id, parent_dom, child_dom, now_unix)
                    )
        except Exception as e:
            print(f"  [lineage_live] error: {e}")


def _run_post_batch_drift_check(ctx):
    """Post-batch advisory drift check + the state summary line."""
    applied = ctx.applied
    state = ctx.state

    if applied > 0:
        # Drift detection: check worker health after batch apply (threat model)
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from drift_detector import run_drift_check
            drift_alerts = run_drift_check()
            if drift_alerts > 0:
                print(f"  ⚠ Drift detector flagged {drift_alerts} worker(s)")
        except Exception:
            pass  # Drift detection is advisory, not blocking

        count = state.get("experiments_completed_list_len", "?")
        print(f"  Updated self_state.json ({count} experiments)")


def apply_results():
    """Apply all unapplied worker results.

    Uses state_write_lock to prevent race conditions with other writers.
    Uses get_db() for fcntl.flock serialization (prevents concurrent write corruption).

    Extracted 2026-07-09 (REFACTORING.md §2): the per-result pipeline now
    runs as _stage_* helpers sharing one _ApplyContext; every stage body was
    moved verbatim from the original monolithic loop, and this function is
    the orchestration. Write discipline is unchanged: commit PER RESULT,
    never across the batch or an LLM/network call.
    """
    with get_db() as conn:
        # Get unapplied results — include new epistemic fields
        results = conn.execute(
            "SELECT id, experiment_id, kanban_task_id, hypothesis_supported, "
            "key_finding, confidence, domain, tags, files_produced, "
            "queue_additions, worker_id, created_at, "
            "predicted_direction, observed_direction, design_vector, experiment_type, "
            "mechanism_type, model, calibrated_confidence, verdict_basis "
            "FROM worker_results WHERE applied = 0 ORDER BY id"
        ).fetchall()

        if not results:
            return 0

        ctx = _ApplyContext(conn=conn)
        # Acquire lock, read latest state, apply changes, save atomically
        with state_write_lock() as state:
            if not state:
                print("WARN: self_state.json not found, skipping state update")
                state = {}
            ctx.state = state

            # Pre-load dedup sets ONCE before the loop (was per-result = O(n*30K) scans)
            _load_dedup_sets(ctx)

            for row in results:
                _begin_result(ctx, row)
                _stage_parse_fields(ctx)
                _stage_shadow_parser(ctx)
                _stage_derive_verdict(ctx)
                _stage_resolve_timestamps(ctx)
                _stage_resolve_hypothesis(ctx)
                _stage_confidence_backstops(ctx)
                _stage_validate_quality(ctx)
                if _stage_reject_low_quality(ctx):
                    continue

                _stage_insert_experiment(ctx)
                _stage_classify_refutation(ctx)
                _stage_attach_claim(ctx)
                _stage_finalize_manifest(ctx)
                _stage_adversarial_routing(ctx)
                _stage_dispute_arbitration(ctx)
                _stage_retest_credit(ctx)

                # 2. Update self_state.json
                if ctx.state:
                    _stage_update_state_counters(ctx)
                    _stage_resolve_parent_curiosity(ctx)
                    _stage_check_generation_throttle(ctx)
                    _stage_insert_queue_additions(ctx)
                    _stage_refutation_branching(ctx)
                    _stage_confirmed_branching(ctx)

                    if ctx.inserted:
                        print(f"  Inserted {ctx.inserted} curiosities into DB")

                    ctx.state["last_updated"] = ctx.now_unix

                # 3. Mark as applied
                conn.execute(
                    "UPDATE worker_results SET applied = 1 WHERE id = ?", (ctx.result_id,)
                )
                ctx.applied += 1
                print(f"  Applied: {ctx.exp_id} — {ctx.key_finding[:60] if ctx.key_finding else 'no finding'}")

                # Transfer tracking: update completion if this task was a transfer.
                # CRITICAL (2026-06-25): pass the OPEN conn so update_completed reuses
                # this transaction instead of opening a competing connection that
                # deadlocks on the write lock we already hold (database is locked →
                # swallowed exception → transfer silently never marked completed).
                if ctx.task_id:
                    update_completed(ctx.task_id, ctx.result_id, ctx.domain, conn=conn)

                _stage_lineage_live_transfer(ctx)

                # Commit per result. Intake is the system's most frequent writer
                # (every minute); the old batch-commit (every 50 APPLIED results)
                # almost never fired mid-run, so the whole batch ran as one
                # deferred transaction and held the WAL write lock for the entire
                # run — the main source of "database is locked" deaths in every
                # other cron writer. Each result is self-contained; a WAL commit
                # at synchronous=NORMAL is sub-millisecond.
                conn.commit()

        # get_db() auto-commits and releases lock on exit

        _run_post_batch_drift_check(ctx)

        print(f"\nApplied {ctx.applied} worker results")

        return ctx.applied


if __name__ == "__main__":
    n = apply_results()
    sys.exit(0 if n >= 0 else 1)
