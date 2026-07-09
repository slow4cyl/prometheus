#!/usr/bin/env python3
"""
synthesis_merger.py — Merge synthesis outputs into self_state.json.

Reads unapplied rows from synthesis_outputs table in prometheus.db.
Applies queue resolutions, new curiosities, and counter overrides
to self_state.json using state_write_lock for serialized access.

Run every 2 minutes via cron. Synthesis workers write to the table;
this script merges to self_state.json.

Architecture:
  synthesis_worker_1 ──┐
  synthesis_worker_2 ──┤──► synthesis_outputs table (SQLite, WAL)
  synthesis_worker_N ──┘         │
                                 ▼
                         synthesis_merger.py (this script)
                                 │
                                 ▼
                         self_state.json (via state_lock)
"""
import json
import os
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone

# ── Hard runtime guard ──────────────────────────────────────────────
# If this script runs longer than MAX_RUNTIME seconds, it's stuck
# (e.g. pathological O(N*M) dedup loop on 40K+ experiments).
# The watchdog-of-watchdogs will also kill stragglers, but a self-
# destruct is cleaner.
_MAX_RUNTIME = 90  # seconds — reduced from 300. Index pre-loading is now O(1) per batch, not per-output.

def _timeout_handler(_signum, _frame):
    elapsed = time.time() - _MERGE_START
    print(f"\nFATAL: synthesis_merger exceeded {_MAX_RUNTIME}s runtime "
          f"(was {elapsed:.0f}s). Possible infinite loop or pathological "
          f"input. Bailing out.")
    # NOTE: os._exit() is mandatory here, not sys.exit(). Python's signal
    # handler dispatch machinery catches SystemExit raised from a signal handler
    # and suppresses it — the process would continue running indefinitely.
    os._exit(1)

signal.signal(signal.SIGALRM, _timeout_handler)
_MERGE_START = time.time()

# Curiosity structural validator (post-generation, pre-queue)
try:
    from curiosity_repair import validate_and_repair, extract_structural_fields, compute_completeness, detect_template
    VALIDATOR_AVAILABLE = True
except ImportError:
    VALIDATOR_AVAILABLE = False

# Multi-objective curiosity scorer
try:
    from curiosity_multi_scorer import score_curiosity as multi_score, load_model as load_scorer_model
    SCORER_MODEL = None  # lazy load
    MULTI_SCORER_AVAILABLE = True
except ImportError:
    MULTI_SCORER_AVAILABLE = False

# Use shared state_lock for serialized write access (prevents race conditions)

"""Synthesis Merger.

Part of the Prometheus research infrastructure.
"""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from state_lock import load_state, save_state, state_write_lock

# Canonical main hermes dir — derived from script location to avoid HOME override issues
_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve HERMES_HOME: use env var if set, but fall back to main dir if profile dir lacks infrastructure
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(os.path.join(_hermes_home_env, "prometheus_db.py")) else _HERMES_MAIN

sys.path.insert(0, HERMES_HOME)
from db_retry import get_db


DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")
STATE_PATH = os.path.join(HERMES_HOME, "self_state.json")


def _ensure_synthesis_curiosity_links(conn):
    """Create the forward provenance ledger for synthesis-generated curiosities."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS synthesis_curiosity_links (
            synthesis_output_id INTEGER NOT NULL,
            synthesis_task_id TEXT NOT NULL,
            curiosity_id INTEGER NOT NULL,
            method TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (synthesis_output_id, curiosity_id)
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_synthesis_curiosity_links_curiosity "
        "ON synthesis_curiosity_links(curiosity_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_synthesis_curiosity_links_task "
        "ON synthesis_curiosity_links(synthesis_task_id)"
    )


def _detect_columns(conn):
    """Detect which column names the synthesis_outputs table uses."""
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(synthesis_outputs)"
    ).fetchall()}
    has_applied = "applied" in cols
    has_task_id = "task_id" in cols
    has_synth_task_id = "synthesis_task_id" in cols
    has_worker_id = "worker_id" in cols
    return {
        "has_applied": has_applied,
        "has_task_id": has_task_id,
        "has_synth_task_id": has_synth_task_id,
        "has_worker_id": has_worker_id,
        "task_id_col": "synthesis_task_id" if has_synth_task_id else "task_id",
        "resolutions_col": "queue_resolutions" if "queue_resolutions" in cols else "resolutions",
        "curiosities_col": "new_curiosities" if "new_curiosities" in cols else "curiosities",
        "patterns_col": "key_patterns" if "key_patterns" in cols else "patterns",
    }


def _get_queue_domain_counts(conn, limit=500):
    """Get domain distribution of active curiosities in the queue.
    
    Used for queue-state feedback: injectors should skip domains that
    already have many pending curiosities, preventing concentration
    overshoot (the 'memorylessness' problem identified in stability analysis).
    
    Returns: {domain: count} for the most recent 'limit' active curiosities.
    """
    domain_counts = {}
    try:
        for row in conn.execute(
            "SELECT e.domain, COUNT(*) as cnt "
            "FROM curiosities c "
            "LEFT JOIN experiments e ON e.id = c.source_experiment "
            "WHERE c.status = 'active' AND c.created_at > ? "
            "GROUP BY e.domain "
            "ORDER BY cnt DESC",
            (int(time.time()) - 7200,)  # last 2 hours
        ).fetchall():
            if row[0]:
                domain_counts[row[0]] = row[1]
    except Exception:
        pass
    return domain_counts


def _domain_is_saturated(domain, queue_domain_counts, threshold=15):
    """Check if a domain already has too many pending curiosities in the queue.
    
    threshold=15 means: if there are already 15+ active curiosities from this
    domain in the last 2 hours, skip generating more. This prevents the
    injector from flooding a single domain with questions (the overshoot
    mechanism identified in the 2026-06-19 stability analysis).
    """
    if not domain:
        return False
    return queue_domain_counts.get(domain, 0) >= threshold


def merge_synthesis_outputs():
    """Apply all unapplied synthesis outputs to self_state.json.

    Uses state_write_lock to prevent race conditions with other writers
    (sync_sqlite_to_state, apply_worker_results).
    """
    # Arm the runtime guard — 120s to finish or die
    signal.alarm(_MAX_RUNTIME)
    with get_db() as conn:
        # Enable WAL mode for better concurrent read/write access
        # (merger can read while synthesis workers write to synthesis_outputs)
        # busy_timeout=300000 (5 min) — the curiosities table is under constant
        # contention from apply_worker_results, sync_sqlite_to_state, and synthesis
        # workers. There's no off-peak window. The merger must wait for the lock
        # to release rather than fail. The MAX_RUNTIME guard (300s) still kills
        # the process if it can't commit in time.
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=300000")
        except Exception:
            pass
        # Detect actual column names (schema drifted between versions)
        _ensure_synthesis_curiosity_links(conn)
        conn.commit()
        schema = _detect_columns(conn)

        # Build query dynamically based on available columns
        task_col = schema["task_id_col"]
        res_col = schema["resolutions_col"]
        cur_col = schema["curiosities_col"]
        pat_col = schema["patterns_col"]

        select_cols = [f"id", task_col, "experiments_covered", res_col, cur_col, pat_col, "created_at"]
        if schema["has_worker_id"]:
            select_cols.insert(2, "worker_id")
        if "counter_overrides" in {row[1] for row in conn.execute("PRAGMA table_info(synthesis_outputs)").fetchall()}:
            select_cols.insert(-1, "counter_overrides")
        if "domain_confidence_updates" in {row[1] for row in conn.execute("PRAGMA table_info(synthesis_outputs)").fetchall()}:
            select_cols.insert(-1, "domain_confidence_updates")

        where_clause = "WHERE 1=1"  # process all rows
        if schema["has_applied"]:
            where_clause = "WHERE applied = 0"

        sql = f"SELECT {', '.join(select_cols)} FROM synthesis_outputs {where_clause} ORDER BY id LIMIT 25"
        outputs = conn.execute(sql).fetchall()

        if not outputs:
            return 0

        # Pre-load dedup sets ONCE before the loop (was per-output = 10x loading 140K items)
        existing_curiosities = set()
        word_to_curiosities = {}  # word -> set of existing_curiosities text_lower
        try:
            # Only load curiosities from last 7 days for dedup (not all 81K).
            # Full dedup is handled by RAG/embedding downstream in task_refiller.
            _dedup_cutoff = int(time.time()) - 86400
            for row in conn.execute(
                "SELECT text FROM curiosities WHERE status='active' AND created_at > ?", (_dedup_cutoff,)
            ):
                if row[0] and len(row[0]) > 10:
                    text_lo = row[0].lower()
                    existing_curiosities.add(text_lo)
                    for w in re.findall(r'\w{4,}', text_lo):
                        if w not in word_to_curiosities:
                            word_to_curiosities[w] = set()
                        word_to_curiosities[w].add(text_lo)
        except Exception:
            pass

        # Pre-load completed hypotheses for dedup (same indexing)
        completed_hyps = set()
        word_to_hyps = {}
        try:
            # Only load experiments from last 7 days for dedup (not all 62K).
            _dedup_cutoff = int(time.time()) - 86400
            for row in conn.execute(
                "SELECT hypothesis FROM experiments WHERE status='completed' AND hypothesis IS NOT NULL AND created_at > ?", (_dedup_cutoff,)
            ):
                if row[0] and len(row[0]) > 20:
                    hyp_lo = row[0].lower()
                    completed_hyps.add(hyp_lo)
                    for w in re.findall(r'\w{4,}', hyp_lo):
                        if w not in word_to_hyps:
                            word_to_hyps[w] = set()
                        word_to_hyps[w].add(hyp_lo)
        except Exception:
            pass

        # Queue-state feedback (2026-06-19 stability fix):
        # Load current domain distribution of active curiosities so injectors
        # can skip domains that are already saturated. This converts the
        # synthesis merger from open-loop (blind injection) to closed-loop
        # (state-aware injection), eliminating the overshoot that causes
        # Gini oscillation. See architecture-changelog.md for analysis.
        queue_domain_counts = _get_queue_domain_counts(conn)
        # Batch cap per merge run (across all outputs). Previously unbounded
        # (25 outputs x 5 curiosities = 125 max per run). Cap at 50 to reduce
        # injector gain by ~2.5x while preserving cross-domain reach.
        SYNTHESIS_RUN_CAP = 50
        run_total_added = 0

        # Acquire lock, read latest state, apply changes, save atomically
        with state_write_lock() as state:
            if not state:
                print("WARN: self_state.json empty or missing")
                return 0

            applied = 0
            for row in outputs:
                # Unpack dynamically based on what columns exist
                row_id = row[0]
                synth_task_id = row[1]  # task_id or synthesis_task_id
                idx = 2
                worker_id = None
                if schema["has_worker_id"]:
                    worker_id = row[idx]
                    idx += 1
                exps_json = row[idx]; idx += 1
                resolutions_json = row[idx]; idx += 1
                curiosities_json = row[idx]; idx += 1
                # Skip optional columns if present
                counters_json = "{}"
                domain_json = "{}"
                if "counter_overrides" in select_cols:
                    counters_json = row[idx]; idx += 1
                if "domain_confidence_updates" in select_cols:
                    domain_json = row[idx]; idx += 1
                patterns = row[idx]; idx += 1
                created_at = row[idx]

                # Parse JSON fields (handle malformed JSON gracefully)
                try:
                    experiments_covered = json.loads(exps_json) if exps_json else []
                    if not isinstance(experiments_covered, list):
                        experiments_covered = []
                except (json.JSONDecodeError, TypeError):
                    experiments_covered = []
                try:
                    queue_resolutions = json.loads(resolutions_json) if resolutions_json else []
                    if not isinstance(queue_resolutions, list):
                        # Column contains a scalar (e.g. '51', '605', '0') instead of JSON array
                        queue_resolutions = []
                except (json.JSONDecodeError, TypeError):
                    queue_resolutions = []
                try:
                    new_curiosities = json.loads(curiosities_json) if curiosities_json else []
                    if not isinstance(new_curiosities, list):
                        new_curiosities = []
                except (json.JSONDecodeError, TypeError):
                    # Handle plain text curiosities — split on semicolons
                    if curiosities_json and len(curiosities_json) > 10:
                        new_curiosities = [c.strip() for c in curiosities_json.split(";") if c.strip() and len(c.strip()) > 10]
                    else:
                        new_curiosities = []
                try:
                    parsed = json.loads(counters_json) if counters_json else {}
                    counter_overrides = parsed if isinstance(parsed, dict) else {}
                except (json.JSONDecodeError, TypeError):
                    counter_overrides = {}
                try:
                    domain_updates = json.loads(domain_json) if domain_json else {}
                except (json.JSONDecodeError, TypeError):
                    domain_updates = {}

                # 1. Apply queue resolutions — mark as resolved in SQLite
                # Use conn (from get_db()) instead of opening a second connection
                # to avoid "database is locked" from fcntl exclusive lock
                resolved_count = 0
                for res in queue_resolutions:
                    if not isinstance(res, dict):
                        continue  # skip malformed entries
                    item_idx = res.get("item")
                    resolved_by = res.get("resolved_by", "")
                    note = res.get("note", "")
                    if resolved_by:
                        # Mark the specific curiosity as resolved in DB
                        try:
                            conn.execute(
                                """UPDATE curiosities SET status='resolved', 
                                   resolved_by_experiment=?, resolved_at=?
                                   WHERE id=?""",
                                (resolved_by, time.time(), item_idx)
                            )
                            resolved_count += 1
                        except Exception:
                            pass
                if resolved_count:
                    conn.commit()
                    print(f"  Resolved {resolved_count} curiosities in DB")

                # 2. Add new curiosities to SQLite (canonical store)
                # Dedup index pre-loaded ONCE before the loop (above)
                items_added = 0
                new_concepts_count = 0

                # THROTTLE (P-001): cap curiosities per synthesis output and require
                # experimental grounding. Synthesis without experiments_covered is
                # speculative — those questions have 0.26% fertility rate.
                SYNTHESIS_CURIOSITY_CAP = 5
                if not exps_json or exps_json == '[]':
                    new_curiosities = []  # no experiments → no questions
                
                # Phase 4.2: Quality gate — filter out questions that are too generic
                # Questions must reference a specific finding/mechanism, not just ask "does X work?"
                import re as _re_filter
                _filtered = []
                for _q in new_curiosities[:SYNTHESIS_CURIOSITY_CAP]:
                    _q_lower = _q.lower()
                    # Reject pure confirmation questions with no specifics
                    if _q_lower in ['does this work?', 'does it transfer?', 'what happens?', 'is this true?']:
                        continue
                    # Reject very short questions
                    if len(_q.strip()) < 30:
                        continue
                    _filtered.append(_q)
                new_curiosities = _filtered

                for curiosity in new_curiosities:
                    text = curiosity if isinstance(curiosity, str) else curiosity.get("text", "")
                    if text and len(text) > 10:
                        # Queue-state feedback: check batch cap
                        if run_total_added >= SYNTHESIS_RUN_CAP:
                            break
                        
                        is_dup = False
                        text_lower = text.lower()
                        item_words = set(re.findall(r'\w{4,}', text_lower))

                        # Queue-state feedback: skip if the source domain is saturated
                        # The synthesis output references experiments_covered; we check
                        # the first experiment's domain against current queue state.
                        _skip_for_saturation = False
                        try:
                            if exps_json:
                                import json as _j
                                _exp_ids = _j.loads(exps_json) if isinstance(exps_json, str) else exps_json
                                if _exp_ids and len(_exp_ids) > 0:
                                    _exp_row = conn.execute(
                                        "SELECT domain FROM experiments WHERE id = ?", (_exp_ids[0],)
                                    ).fetchone()
                                    if _exp_row and _exp_row[0]:
                                        if _domain_is_saturated(_exp_row[0], queue_domain_counts):
                                            _skip_for_saturation = True
                        except Exception:
                            pass
                        
                        if _skip_for_saturation:
                            continue

                        # Index-based dedup against existing active curiosities
                        if item_words:
                            # Get candidate set: only items sharing at least one word
                            candidates = set()
                            for w in item_words:
                                if w in word_to_curiosities:
                                    candidates.update(word_to_curiosities[w])
                            # Only check candidates (not all 28K)
                            for existing_text in candidates:
                                existing_words = set(re.findall(r'\w{4,}', existing_text))
                                if item_words and existing_words:
                                    overlap = len(item_words & existing_words) / max(len(item_words | existing_words), 1)
                                    if overlap > 0.55:
                                        is_dup = True
                                        break

                        # Index-based dedup against completed experiments
                        if not is_dup and completed_hyps and len(item_words) >= 3:
                            candidates = set()
                            for w in item_words:
                                if w in word_to_hyps:
                                    candidates.update(word_to_hyps[w])
                            for hyp in candidates:
                                hyp_words = set(re.findall(r'\w{4,}', hyp))
                                if hyp_words:
                                    overlap = len(item_words & hyp_words) / max(len(item_words | hyp_words), 1)
                                    if overlap > 0.5:
                                        is_dup = True
                                        break

                        if not is_dup:
                            # --- Structural validation and A/B repair ---
                            repair_status = 'none'
                            completeness_score = 4
                            template_type = 'unknown'
                            if VALIDATOR_AVAILABLE:
                                try:
                                    fields = extract_structural_fields(text)
                                    completeness_score = compute_completeness(fields)
                                    template_type = detect_template(text)
                                    if completeness_score <= 2:
                                        # A/B split: deterministic hash-based assignment
                                        import hashlib
                                        h = int(hashlib.md5(text.encode()).hexdigest(), 16) % 100
                                        if h < 50:  # 50% get repair
                                            result = validate_and_repair(text)
                                            if result.get('repaired') and result['repaired'] != text:
                                                text = result['repaired']
                                                repair_status = 'repaired'
                                            else:
                                                repair_status = 'no_repair_needed'
                                        else:
                                            repair_status = 'control'
                                    else:
                                        repair_status = 'complete'
                                except Exception:
                                    repair_status = 'error'
                            # --- End validation ---

                            # Check for new vocabulary (concept formation signal)
                            new_phrases = re.findall(r'[a-z]+ [a-z]+ [a-z]+', text_lower)
                            if new_phrases:
                                abstraction_terms = ['adaptive', 'heterogeneous', 'population', 'selection',
                                                     'constraint', 'feedback', 'regulation', 'phase',
                                                     'emergence', 'self-organiz', 'critical', 'threshold']
                                if any(term in phrase for phrase in new_phrases for term in abstraction_terms):
                                    new_concepts_count += 1

                            # Insert into SQLite
                            # Multi-objective scoring
                            p_confirm = None
                            p_novel = None
                            p_expand = None
                            combined_score = None
                            if MULTI_SCORER_AVAILABLE:
                                try:
                                    global SCORER_MODEL
                                    if SCORER_MODEL is None:
                                        SCORER_MODEL = load_scorer_model()
                                    if SCORER_MODEL:
                                        ms = multi_score(text, SCORER_MODEL)
                                        p_confirm = ms.get('p_confirm')
                                        p_novel = ms.get('p_novel')
                                        p_expand = ms.get('p_expand')
                                        combined_score = ms.get('combined')
                                except Exception:
                                    pass

                            # Find parent curiosity from experiments_covered
                            parent_curiosity_id = None
                            try:
                                if exps_json:
                                    import json as _json
                                    exp_ids = _json.loads(exps_json) if isinstance(exps_json, str) else exps_json
                                    if exp_ids and len(exp_ids) > 0:
                                        exp_row = conn.execute(
                                            "SELECT hypothesis FROM experiments WHERE id = ?", (exp_ids[0],)
                                        ).fetchone()
                                        if exp_row and exp_row[0]:
                                            prow = conn.execute(
                                                "SELECT id FROM curiosities WHERE text LIKE ? ORDER BY id DESC LIMIT 1",
                                                (exp_row[0][:60] + "%",)
                                            ).fetchone()
                                            if prow:
                                                parent_curiosity_id = prow[0]
                            except Exception:
                                pass

                            _throttled = False
                            try:
                                from generation_throttle import should_throttle
                                _throttled = should_throttle(conn)
                            except Exception:
                                pass
                            
                            if _throttled:
                                break
                            
                            now_ts = time.time()
                            conn.execute(
                                """INSERT INTO curiosities (text, priority, status, source_experiment, created_at,
                                                           repair_status, template_type, completeness_score,
                                                           p_confirm, p_novel, p_expand, combined_score,
                                                           parent_curiosity_id)
                                   VALUES (?, 3, 'active', 'synthesis', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (text, now_ts, repair_status, template_type, completeness_score,
                                 p_confirm, p_novel, p_expand, combined_score, parent_curiosity_id)
                            )
                            curiosity_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]  # pyright: ignore[reportOptionalMemberAccess]
                            conn.execute(
                                """INSERT OR IGNORE INTO synthesis_curiosity_links
                                   (synthesis_output_id, synthesis_task_id, curiosity_id, method, created_at)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (row_id, synth_task_id or "", curiosity_id, "synthesis_merger_insert", now_ts)
                            )
                            existing_curiosities.add(text_lower)
                            items_added += 1
                            run_total_added += 1
                            # Update queue domain count so subsequent items see the update
                            if exps_json:
                                try:
                                    import json as _j2
                                    _eids = _j2.loads(exps_json) if isinstance(exps_json, str) else exps_json
                                    if _eids:
                                        _er = conn.execute("SELECT domain FROM experiments WHERE id=?", (_eids[0],)).fetchone()
                                        if _er and _er[0]:
                                            queue_domain_counts[_er[0]] = queue_domain_counts.get(_er[0], 0) + 1
                                except Exception:
                                    pass

                # No separate commit/close needed — get_db() context manager handles it

                # Record queue flow
                if items_added > 0:
                    try:
                        import health_signal
                        health_signal.record_queue_add(items_added)
                    except Exception:
                        pass

                # Track new concepts generated (vocabulary not in existing ontology)
                if new_concepts_count > 0:
                    try:
                        metrics = state.get("metrics", {})
                        metrics["new_concepts_generated"] = metrics.get("new_concepts_generated", 0) + new_concepts_count
                        state["metrics"] = metrics
                    except Exception:
                        pass

                # Queue is now in SQLite — sync_curiosity_views.py handles JSON
                # state["curiosity_queue"] = queue  # REMOVED: SQLite is canonical

                # 3. Apply counter overrides (from synthesis worker's reconciliation)
                if counter_overrides:
                    # Direct field updates
                    for key, value in counter_overrides.items():
                        state[key] = value
                    # Update nested metrics
                    metrics = state.get("metrics", {})
                    for key in ["experiments_completed", "experiments_conducted",
                                "experiments_completed_count"]:
                        if key in counter_overrides:
                            metrics[key] = counter_overrides[key]
                    state["metrics"] = metrics
                    # Update nested counters
                    counters = state.get("counters", {})
                    for key in counter_overrides:
                        if "experiment" in key.lower() or "count" in key.lower():
                            counters[key] = counter_overrides[key]
                    state["counters"] = counters
                else:
                    # Auto-reconcile: use experiments_completed_list length as ground truth
                    ecl = state.get("experiments_completed_list", [])
                    count = len(ecl)
                    state["experiments"] = count
                    state["experiments_completed"] = count
                    state["total_experiments_completed"] = count
                    state["total_experiments"] = count
                    state["experiments_completed_count"] = count
                    state["count"] = count
                    state["completed_count"] = count
                    state["experiments_completed_list_len"] = count
                    state["completed"] = count
                    metrics = state.get("metrics", {})
                    metrics["experiments_completed"] = count
                    metrics["experiments_conducted"] = count
                    metrics["experiments_completed_count"] = count
                    state["metrics"] = metrics
                    counters = state.get("counters", {})
                    counters["experiments_completed"] = count
                    counters["experiments_total"] = count
                    counters["total_experiments"] = count
                    counters["experiments_completed_count"] = count
                    counters["count"] = count
                    state["counters"] = counters

                # 4. Apply domain confidence updates
                if domain_updates:
                    domains = state.get("domains", {})
                    if isinstance(domains, dict):
                        for domain, confidence in domain_updates.items():
                            if isinstance(domains.get(domain), dict):
                                domains[domain]["confidence"] = confidence
                            else:
                                domains[domain] = {"confidence": confidence}
                        state["domains"] = domains

                # 5. Log key patterns (append to audit trail)
                if patterns:
                    audit_trail = state.get("audit_trail_synthesis", [])
                    if not isinstance(audit_trail, list):
                        audit_trail = []
                    audit_trail.append({
                        "synthesis_task": synth_task_id,
                        "worker": worker_id,
                        "experiments": experiments_covered,
                        "patterns": patterns[:500],  # truncate for safety
                        "timestamp": created_at or datetime.now(timezone.utc).isoformat()
                    })
                    # Keep last 50 entries
                    state["audit_trail_synthesis"] = audit_trail[-50:]

                # 6. Update last_updated and last_synthesis timestamps
                state["last_updated"] = datetime.now(timezone.utc).isoformat()
                state["last_synthesis"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

                # Commit per-output so the lock is released between outputs.
                # Other writers can proceed while we process the next output.
                # The 5-minute busy_timeout handles the case where a lock holder
                # is slow to release.
                try:
                    conn.commit()
                except Exception:
                    pass
                # Mark this output as applied so we don't reprocess it
                try:
                    conn.execute("UPDATE synthesis_outputs SET applied=1 WHERE id=?", (row_id,))
                    conn.commit()
                except Exception:
                    pass
                applied += 1
                exp_count = len(experiments_covered)
                cur_count = len(new_curiosities)
                print(f"  Applied: {synth_task_id} — {exp_count} exps, {cur_count} new curiosities")

        # Lock released and state saved by context manager
        # get_db() auto-commits on exit

        if applied > 0:
            # Auto-snapshot after synthesis changes (threat model Item 4)
            try:
                import synthesis_versioning
                snapshot_conn = get_db(DB_PATH)
                try:
                    snapshot_conn.execute("PRAGMA busy_timeout=30000")
                    snapshot_conn.execute("PRAGMA synchronous=NORMAL")
                except Exception:
                    pass
                synthesis_versioning.snapshot_current_state(snapshot_conn)
                snapshot_conn.close()
            except Exception as e:
                print(f"  WARNING: Auto-snapshot failed: {e}")

            count = state.get("experiments_completed_list_len",
                               state.get("experiments_completed", "?")) if state else "?"
            queue_size = state.get("curiosity_queue_size", "?")
            print(f"  Updated self_state.json — {count} experiments, {queue_size} queue items")

        print(f"\nMerged {applied} synthesis outputs")
        return applied


if __name__ == "__main__":
    n = merge_synthesis_outputs()
    sys.exit(0 if n >= 0 else 1)
