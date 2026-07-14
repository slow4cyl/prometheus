#!/usr/bin/env python3
"""
task_refiller.py — Fast loop: maintain queue depth.

Runs every 60s via cron (script-only, no LLM). Reads ready count from
kanban.db, creates tasks to fill the pool toward READY_TARGET_DEPTH,
injects novelty when the genuine lane underfills, and writes a summary
JSON for the Director to read.

This script has ZERO policy. It maintains depth. That's it.
"""
import os as _os
_os.environ.setdefault('OMP_THREAD_LIMIT', '1')
_os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
_os.environ.setdefault('MKL_NUM_THREADS', '1')

import json
from prometheus_paths import HERMES_HOME as _PP_HERMES_HOME
import fcntl
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter


def get_task_priority(item, lane):
    """Map task type to kanban priority. Higher = dispatched first.

    The kanban dispatches ready tasks by ORDER BY priority DESC, created_at ASC.
    This function assigns the right priority when creating kanban tasks, so the
    kanban's native ordering handles dispatch without carve-outs.

    Tiers:
      5 — RESERVED for genuine emergencies (no automated source assigns it)
      4 — BENCH3 (external truth anchor — calibration depends on it)
      3 — DEEP_LINEAGE (the depth-climb — the system's product)
      2 — SYNTHESIS / COMPRESSION / CANDIDATE_RETEST (abstraction + validation)
      1 — TRANSFER / REFILLER / INJECTION / PROBES (exploration mass)
      0 — everything else
    """
    if not isinstance(item, dict):
        return 0

    # Tier 4: BENCH3 ground truth (P5 left vacant for manual emergency dispatch)
    if item.get("source") == "bench3_ground_truth" or item.get("benchmark_id"):
        return 4

    text = (item.get("text", "") or "").lstrip()
    src = item.get("source", "")

    # Tier 3: Deep lineage (evidence_depth >= 10). The depth-climb — the system's
    # actual product. Highest non-benchmark priority. Checked before everything
    # below so deep items always win a dispatch slot over validation/exploration.
    if item.get("synthesis_deep"):
        return 3

    # Tier 2: Abstraction + validation housekeeping. Synthesis/compression and
    # candidate-retests (background claim validation). These run behind the deep
    # climb, ahead of raw exploration mass.
    # DRAIN MODE (2026-07-02): candidate-retests ride at tier 3 — at tier 2
    # they lost every dispatch slot to the deep climb's p3 trickle and the
    # drain ran at ~15% of the worker pool despite 18 in-flight slots. Equal
    # priority + FIFO means the (much larger) ready retest set dominates
    # dispatch until the pool drains; the lane's in-flight target then idles
    # it back down. Revert to 2 if retests should yield to the climb again.
    if text.upper().startswith("[CANDIDATE-RETEST]"):
        return 3
    if src in ("synthesis", "compression_synthesis"):
        return 2
    if text.upper().startswith(("SYNTHESIS:", "[SYNTHESIS", "[COMPRESSION", "COMPRESSION-")):
        return 2

    # Tier 1: Active exploration pressure. Transfers, generic refiller items,
    # injections, probes. Generic refiller = genuine-lane item with no special
    # source tag (the [REFILLER] prefix is on the title, not item text).
    if text.startswith(("[TRANSFER]", "[TRANSFER from")):
        return 1
    if src in ("opportunity_injection", "external_probe"):
        return 1
    if lane == "genuine" and src not in ("synthesis", "compression_synthesis",
                                          "opportunity_injection", "external_probe",
                                          "bench3_ground_truth"):
        return 1

    # Tier 0: Residual / long-tail exploration
    return 0
from datetime import datetime, timezone

# Paths
HERMES_HOME = _PP_HERMES_HOME
KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")
SUMMARY_PATH = os.path.join(HERMES_HOME, "refiller_summary.json")
SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")
LOCK_PATH = os.path.join(HERMES_HOME, ".task_refiller.lock")

sys.path.insert(0, SCRIPTS_DIR)

# Use db_retry for all database access (prevents "database is locked" errors)
from db_retry import get_db as retry_get_db

# Transfer tracking: link tasks to transfer_tracking rows
try:
    from transfer_tracking import update_task_created, update_task_created_by_domain, parse_transfer_target
    TRANSFER_TRACKING_AVAILABLE = True
except ImportError:
    TRANSFER_TRACKING_AVAILABLE = False

# Import worker config
try:
    from worker_config import (
        READY_TARGET_DEPTH,
        MAX_CREATE_PER_CYCLE,
        GENUINE_SHARE,
        WORKER_COUNT,
        create_budget,
        lane_plan,
    )
except ImportError:
    print("ERROR: Cannot import worker_config. Using defaults.")
    READY_TARGET_DEPTH = 150
    MAX_CREATE_PER_CYCLE = 50
    GENUINE_SHARE = 0.60
    WORKER_COUNT = 50
    def create_budget(current_ready=0):
        deficit = max(0, READY_TARGET_DEPTH - max(0, current_ready))
        return min(deficit, MAX_CREATE_PER_CYCLE)
    def lane_plan(count=None, current_ready=None):
        if current_ready is not None:
            target = create_budget(current_ready)
        elif count is not None:
            target = count
        else:
            target = MAX_CREATE_PER_CYCLE
        if target <= 0:
            return [("genuine", 0), ("transfer", 0)]
        genuine = round(target * GENUINE_SHARE)
        if target >= 2:
            genuine = min(max(genuine, 1), target - 1)
        transfer = target - genuine
        return [("genuine", genuine), ("transfer", transfer)]


def get_ready_count():
    """Current number of ready tasks in kanban."""
    try:
        conn = retry_get_db(KANBAN_DB)
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='ready'"
        ).fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"WARNING: Failed to read ready count: {e}")
        return 0


def get_running_count():
    """Current number of running tasks."""
    try:
        conn = retry_get_db(KANBAN_DB)
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running'"
        ).fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def get_recent_rates():
    """Compute creation and consumption rates over the last hour."""
    now = int(time.time())
    hour_ago = now - 3600
    try:
        conn = retry_get_db(KANBAN_DB)
        created = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_at >= ?", (hour_ago,)
        ).fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE completed_at >= ?", (hour_ago,)
        ).fetchone()[0]
        conn.close()
        # Per-minute rates
        creation_rate = created / 60.0
        consumption_rate = completed / 60.0
        velocity = creation_rate - consumption_rate
        return {
            "created_last_hour": created,
            "completed_last_hour": completed,
            "creation_rate": round(creation_rate, 2),
            "consumption_rate": round(consumption_rate, 2),
            "velocity": round(velocity, 2),
        }
    except Exception as e:
        return {
            "created_last_hour": 0,
            "completed_last_hour": 0,
            "creation_rate": 0,
            "consumption_rate": 0,
            "velocity": 0,
        }


def compute_entropy():
    """Compute queue entropy from curiosity queue."""
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from curiosity_scorer import classify_thread, load_state
        state = load_state()
        queue = state.get("curiosity_queue", [])
        if not queue:
            return 0.0, {}
        threads = [
            classify_thread(
                str(item) if not isinstance(item, dict)
                else item.get("text", item.get("question", str(item)))
            )
            for item in queue
        ]
        freq = Counter(threads)
        total = len(threads)
        if total == 0:
            return 0.0, {}
        entropy = -sum(
            (c / total) * math.log2(c / total)
            for c in freq.values() if c > 0
        )
        return round(entropy, 3), freq
    except Exception as e:
        return 0.0, {}


def get_top_domains():
    """Get domain distribution of recent tasks."""
    try:
        conn = retry_get_db(KANBAN_DB)
        rows = conn.execute(
            """SELECT title FROM tasks
               WHERE created_at >= ? AND status != 'archived'""",
            (int(time.time()) - 3600,)
        ).fetchall()
        conn.close()
        # Simple keyword-based domain extraction
        domain_counts = {}
        for (title,) in rows:
            if not title:
                continue
            title_lower = title.lower()
            # Extract domain hints from [TRANSFER from X] patterns
            if "[transfer from " in title_lower:
                try:
                    domain = title_lower.split("[transfer from ")[1].split("]")[0].strip()
                    domain_counts[domain] = domain_counts.get(domain, 0) + 1
                except (IndexError, ValueError):
                    pass
        return dict(sorted(domain_counts.items(), key=lambda x: -x[1])[:5])
    except Exception:
        return {}


def run_batch_create(lane, count):
    """Run batch_create_tasks.py for one lane. Returns (created, refiller_created, output)."""
    if count <= 0:
        return 0, 0, ""
    # Fast path: if pool is below target, create directly via DB (skips dedup)
    ready = get_ready_count()
    if ready < READY_TARGET_DEPTH:
        return direct_db_create(lane, count)

    # Normal path: use batch_create with full dedup pipeline
    try:
        result = subprocess.run(
            [
                "python3",
                os.path.join(SCRIPTS_DIR, "batch_create_tasks.py"),
                "--count", str(count),
                "--min-score", "30",
                "--only", lane,
            ],
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "HERMES_HOME": HERMES_HOME},
        )
        output = result.stdout or ""
        # Parse "Created N/M tasks" or "Rapid-fill created N/M tasks"
        import re
        m = re.search(r"(?:Created|Rapid-fill created)\s+(\d+)/", output)
        created = int(m.group(1)) if m else 0
        return created, 0, output  # subprocess path doesn't surface refiller_created
    except subprocess.TimeoutExpired:
        print(f"WARNING: batch_create timed out for lane={lane}")
        return 0, 0, ""
    except Exception as e:
        print(f"WARNING: batch_create failed for lane={lane}: {e}")
        return 0, 0, ""


def direct_db_create(lane, count):
    """Create tasks directly via DB, skipping dedup. Fast path for pool filling."""
    import uuid
    try:
        # BENCH3 GROUND TRUTH: pull unanswered BENCH3 questions with known answers
        # first. These feed the calibration loop with external truth.
        _bench3_tasks = []
        if lane == "genuine":
            try:
                import sqlite3 as _b3_sqlite
                _b3_db = _b3_sqlite.connect(os.path.join(HERMES_HOME, "prometheus.db"), timeout=5)
                _b3_db.row_factory = _b3_sqlite.Row
                _b3_rows = _b3_db.execute("""
                    SELECT id, text, benchmark_id FROM curiosities
                    WHERE benchmark_id LIKE 'BENCH3-%'
                      AND known_answer IN ('true', 'false')
                      AND source_experiment IS NULL
                      AND status = 'active'
                      AND created_at < 9e200
                    ORDER BY created_at ASC
                """).fetchall()
                _b3_db.close()
                # Filter out curiosities that already have kanban tasks
                # BATCH FIX: one query for all bodies, then Python membership check.
                # Was: N individual LIKE queries (O(N*117K) scans). Now: 1 query + O(N) set check.
                if _b3_rows:
                    try:
                        _kanban = retry_get_db(KANBAN_DB)
                        _all_bodies = " ".join(
                            r[0] or "" for r in _kanban.execute(
                                "SELECT body FROM tasks WHERE status IN ('ready','running')"
                            ).fetchall()
                        )
                        _kanban.close()
                        _dispatched_ids = set()
                        for _b3 in _b3_rows:
                            _cid_str = str(_b3["id"])
                            if f"CURIOSITY_ID: {_cid_str}" in _all_bodies:
                                _dispatched_ids.add(_b3["id"])
                        _b3_rows = [r for r in _b3_rows if r["id"] not in _dispatched_ids]
                    except Exception:
                        pass  # Fail open — if kanban check fails, allow dispatch
                for _b3 in _b3_rows:
                    _bench3_tasks.append({
                        "curiosity_id": _b3["id"],
                        "text": _b3["text"],
                        "source": "bench3_ground_truth",
                        "benchmark_id": _b3["benchmark_id"],
                    })
                if _bench3_tasks:
                    print(f"BENCH3 GROUND TRUTH: {len(_bench3_tasks)} items for {lane} lane", flush=True)
            except Exception as _b3_err:
                print(f"BENCH3 pull failed (non-fatal): {_b3_err}", file=sys.stderr)
        
        # Load curiosity queue
        state_path = os.path.join(HERMES_HOME, "self_state.json")
        if not os.path.exists(state_path):
            return 0, "No self_state.json"
        with open(state_path) as f:
            state = json.load(f)
        queue = state.get("curiosity_queue", [])
        if not queue:
            return 0, "Empty queue"

        # Filter by lane
        if lane == "genuine":
            items = [q for q in queue if not (isinstance(q, dict) and "[TRANSFER" in str(q.get("text", q)))]
        else:  # transfer
            items = [q for q in queue if isinstance(q, dict) and "[TRANSFER" in str(q.get("text", q))]

        if not items and not _bench3_tasks:
            return 0, f"No {lane} items in queue"

        # Get existing titles to avoid duplicates. Two subtleties, both learned
        # the hard way (2026-07-02: 35 retest curiosities were minted 2-5 tasks
        # EACH — 58% of the drain lane's completions were duplicates):
        #   * membership below tests title[:80] — the stored set and the probe
        #     MUST use the same truncation. This set held [:80] while the check
        #     compared the full (up to 200-char) title, so any title longer
        #     than 80 chars NEVER matched and dedup was dead for exactly the
        #     long-titled lanes (every [CANDIDATE-RETEST] title qualifies).
        #   * include recently-completed tasks, not just ready/running: a task
        #     completes ~1-2 min before intake resolves its curiosity, and the
        #     queue file can carry the still-active curiosity for up to 5 min —
        #     without the completed titles, that window re-mints the same item.
        conn = retry_get_db(KANBAN_DB)
        existing = set()
        try:
            rows = conn.execute(
                "SELECT title FROM tasks WHERE status IN ('ready','running') "
                "OR COALESCE(completed_at, 0) > ?", (time.time() - 1800,)).fetchall()
            existing = {r[0][:80] for r in rows}
        except Exception:
            pass

        # Create tasks with round-robin worker assignment
        # Skip duplicates and keep looking for unique items
        created = 0
        refiller_created = 0  # counter for [refiller]-tagged tasks (2026-06-23 floor)
        now = int(time.time())
        # Get next worker via round-robin (count existing tasks per worker)
        worker_counts = {}
        try:
            for row in conn.execute(
                "SELECT assignee, COUNT(*) as cnt FROM tasks "
                "WHERE status IN ('ready','running') AND assignee IS NOT NULL "
                "GROUP BY assignee"
            ):
                worker_counts[row[0]] = row[1]
        except Exception:
            pass
        # Build sorted worker list — use WORKER_COUNT from worker_config
        try:
            from worker_config import WORKER_COUNT
        except Exception:
            WORKER_COUNT = 24
        workers = ["default"]
        # Pick least-loaded worker
        next_worker = min(workers, key=lambda w: worker_counts.get(w, 0))

        # Add BENCH3 ground truth items to the list. Priority system handles
        # dispatch ordering — no prepend needed.
        if _bench3_tasks and lane == "genuine":
            items = items + _bench3_tasks

        # Maintain a candidate-retest sub-pool at the front of the genuine
        # budget. The queue slice in sync_curiosity_views places retests right
        # behind the deep-lineage block, but that block is an effectively
        # infinite front (16k+ active depth>=10 curiosities, re-topped to 20
        # every sync), so a front-first scan with this cycle's small budget
        # (often 1 genuine slot) never reaches them — measured 0 retest
        # dispatches/hour while 89 deep items dispatched. Gating the reservation
        # on budget size fails: the budget is almost always 1-2. Instead keep a
        # small fixed number of retests in flight — reserve front slots for them
        # only while ready+running holds fewer than RETEST_IN_FLIGHT_TARGET, then
        # yield the budget back to the depth-climb. Over many cycles this is a
        # steady retest trickle that cannot be starved by the deep flood and
        # cannot itself starve it.
        if lane == "genuine":
            # Reserved sub-pools, generalized (retests were the prototype).
            # Each (text-prefix, in-flight target) lane gets front slots only
            # while ready+running holds fewer than its target, then yields the
            # budget back to the depth-climb. Targets:
            #   [CANDIDATE-RETEST] 18 — full-drain mode (user: "why does the
            #     system have to wait days"): the blocked pool is compute-
            #     bound, not timer-bound, so give the drain most of the pool;
            #     the frontier slows until the pool empties, then the deficit
            #     stays 0 and the lane idles back down on its own.
            #   [BOUNDARY] 12 — narrowed-attack boundary mapping; with
            #     combined scores ~26-29 these NEVER win queue positions
            #     against the mass lanes (0 dispatched ever at target 2, same
            #     starvation retests had). Drain mode 2026-07-04: 577 active
            #     boundary questions gate re-attacks on 309 REPLICATED claims
            #     (the ESTABLISHED bottleneck) — surge to 12 until the pool
            #     drains, then the deficit self-throttles; revert to ~4 at
            #     steady state.
            _LANE_TARGETS = (("[CANDIDATE-RETEST]", 18), ("[BOUNDARY]", 12),
                             ("[SPLIT]", 3))
            _front = []
            for _pfx, _target in _LANE_TARGETS:
                _lane_items = [i for i in items if isinstance(i, dict)
                               and str(i.get("text", "")).lstrip().upper().startswith(_pfx)]
                if not _lane_items:
                    continue
                try:
                    _in_flight = conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE status IN ('ready','running') "
                        "AND title LIKE ?", (f"%{_pfx}%",)).fetchone()[0]
                except Exception:
                    _in_flight = 0
                _deficit = _target - _in_flight
                if _deficit > 0:
                    _front.extend(_lane_items[:_deficit])
            if _front:
                _front_ids = {id(i) for i in _front}
                items = _front + [i for i in items if id(i) not in _front_ids]
                # Reserved-lane deficits are budget-EXEMPT. The genuine budget
                # is almost always 1-2 per cycle, so a 15-slot retest deficit
                # would ramp at ~1/cycle and never reach target — the lanes'
                # own in-flight targets are the real bound (18+2 max), and
                # they self-throttle to zero once their pools drain.
                _budget_exempt_ids = _front_ids
            else:
                _budget_exempt_ids = set()
        else:
            _budget_exempt_ids = set()
        for item in items:  # Scan ALL items, not just first count
            if created >= count:
                if not _budget_exempt_ids:
                    break
                if id(item) not in _budget_exempt_ids:
                    continue
            text = item.get("text", item.get("question", str(item))) if isinstance(item, dict) else str(item)
            # Title prefix: makes inventory queries findable by title scan.
            #   [BENCH3-T-1210]   <- BENCH3 ground truth (highest priority)
            #   [synthesis]       <- synthesis_merger output
            #   [injection]       <- inject_opportunities (CROSS-DOMAIN NOVELTY)
            #   [adversarial]     <- adversarial replication tasks
            #   [compression]     <- compression_synthesis validation
            #   [boundary]        <- boundary-condition questions
            #   [transfer]        <- [TRANSFER] lineage question
            #   [refiller]        <- generic task_refiller question
            #   [build]           <- build pipeline tasks
            #   [followup]        <- follow-up from prior task
            # Without these, the lineage marker only lives in the body — invisible to title scan.
            _title_prefix = ""
            if isinstance(item, dict):
                if item.get("benchmark_id"):
                    _title_prefix = f"[{item['benchmark_id']}] "
                elif item.get("template_type"):
                    _tt = item['template_type']
                    if _tt in ('boundary', 'threshold', 'contrast', 'open_investigation'):
                        _title_prefix = f"[{_tt}] "
                elif item.get("source") == "synthesis":
                    _title_prefix = "[synthesis] "
                elif item.get("source") == "compression_synthesis":
                    _title_prefix = "[compression] "
                elif item.get("source") == "opportunity_injection":
                    _title_prefix = "[injection] "
                elif item.get("provenance"):
                    # Inject opportunities + refutation_boost store provenance as JSON
                    # in the curiosities table (no dedicated source column).
                    _prov = item["provenance"]
                    if isinstance(_prov, str):
                        if '"origin": "synthesis"' in _prov or _prov == "synthesis":
                            _title_prefix = "[synthesis] "
                        elif '"origin": "opportunity_injection"' in _prov or _prov == "opportunity_injection":
                            _title_prefix = "[injection] "
                        elif '"origin": "refutation_boost"' in _prov or _prov == "refutation_boost":
                            _title_prefix = "[injection] "
                        elif '"origin": "hidden_bridge_injection"' in _prov or _prov == "hidden_bridge_injection":
                            _title_prefix = "[injection] "
                        elif '"origin": "transfer"' in _prov or _prov == "transfer":
                            _title_prefix = "[transfer] "
                        elif '"origin": "refutation_followup"' in _prov:
                            _title_prefix = "[refutation_followup] "
                        elif '"origin": "confirmed_followup"' in _prov:
                            _title_prefix = "[confirmed_followup] "
            if not _title_prefix and isinstance(item, dict) and item.get("text"):
                # Body-pattern fallback (for tasks whose body fingerprints the script)
                _t = item['text'].lstrip()[:60]
                if _t.upper().startswith("CROSS-DOMAIN NOVELTY"):
                    _title_prefix = "[injection] "
                elif _t.upper().startswith("ADVERSARIAL REPLICATION TASK"):
                    _title_prefix = "[adversarial] "
                elif _t.upper().startswith("BUILD DEPLOYMENT PIPELINE"):
                    _title_prefix = "[build] "
                elif _t.upper().startswith(("SYNTHESIS:", "SYNTHESIS CYCLE", "SYNTHESIS TASK")):
                    _title_prefix = "[synthesis] "
                elif _t.upper().startswith("VALIDATE:") or _t.upper().startswith("VALIDATION TASK"):
                    _title_prefix = "[compression] "
                elif _t.upper().startswith("[COMPRESSION") or _t.upper().startswith("COMPRESSION-BOUNDARY"):
                    _title_prefix = "[compression] "
                elif "COMPRESSION" in _t.upper():
                    _title_prefix = "[compression] "
                elif _t.startswith(("[TRANSFER]", "[TRANSFER from")):
                    _title_prefix = "[transfer] "
            if not _title_prefix:
                _title_prefix = "[refiller] "
            # Skip the bracket prefix if the text already starts with a bracketed
            # semantic marker (e.g. "[TRANSFER from X] ..."). Adding a redundant
            # [transfer] / [injection] on top creates double tags like
            # "[transfer] [TRANSFER from biology] ...". The in-text marker is the
            # authoritative one — it carries the source-domain info.
            _text_stripped = text.lstrip()
            if _text_stripped.startswith(("[TRANSFER from", "[TRANSFER]", "[SYNTHESIS", "[CROSS-DOMAIN", "[ADVERSARIAL", "[BUILD", "[VALIDATE", "[VALIDATION", "[COMPRESSION", "COMPRESSION-")):
                _title_prefix = ""
            # Normalize the category tag to UPPERCASE (2026-06-23): tags were a mix
            # of [synthesis]/[refiller]/[transfer] (lowercase) and in-text markers
            # like [TRANSFER]/[COMPRESSION] (uppercase). Uppercase the bracketed
            # prefix only — the question text after the prefix is left untouched.
            if _title_prefix.startswith("[") and "]" in _title_prefix:
                _rb = _title_prefix.index("]")
                _title_prefix = "[" + _title_prefix[1:_rb].upper() + _title_prefix[_rb:]
            title = (_title_prefix + text)[:200]
            if title[:80] in existing:  # same truncation as the set — see above
                continue  # Skip duplicate, keep looking
            # Simple body generation — include CURIOSITY_ID for lineage tracking
            # Look up the curiosity ID and source_result_id from the DB by text match
            _cid = ""
            _source_result_id = None
            _source_domain = None
            _retest_moot = False
            try:
                _pdb = retry_get_db(os.path.join(HERMES_HOME, "prometheus.db"))
                _prow = _pdb.execute(
                    "SELECT id, source_result_id, source_experiment FROM curiosities WHERE text LIKE ? AND status='active' ORDER BY id DESC LIMIT 1",
                    (text[:60] + "%",)
                ).fetchone()
                if _prow:
                    _cid = _prow[0]
                    _source_result_id = _prow[1]
                    # Sibling-curiosity guard: a retest whose SOURCE experiment
                    # already holds its credit (earned via a DIFFERENT
                    # curiosity's task) can never earn another —
                    # replication_results is UNIQUE(original_experiment_id).
                    # 1g resolves only the curiosity its own task points at,
                    # so such siblings stay active; skip minting from them.
                    if (_prow[2] and text.lstrip().upper().startswith("[CANDIDATE-RETEST]")
                            and _pdb.execute(
                                "SELECT 1 FROM replication_results WHERE original_experiment_id = ? LIMIT 1",
                                (_prow[2],)).fetchone()):
                        _retest_moot = True
                # FIX (2026-06-24): Transfer curiosities created by synthesis_merger have
                # source_result_id=NULL on the child (synthesis doesn't set it). 106/110
                # active [TRANSFER] curiosities are NULL-srid children, so the guard at
                # line ~578 (if _source_result_id and lane=="transfer") never fired, and
                # update_task_created was never called — breaking the entire transfer
                # tracking pipeline (12,141 rows stuck in 'queued' with task_id=NULL,
                # zero transfers completed since June 21). Walk up the parent chain to
                # find the ancestor that carries the real source_result_id.
                if _cid and not _source_result_id:
                    _prow2 = _pdb.execute(
                        """WITH RECURSIVE chain(id, parent, srid, depth) AS (
                             SELECT id, parent_curiosity_id, source_result_id, 0
                             FROM curiosities WHERE id = ?
                             UNION ALL
                             SELECT c.id, c.parent_curiosity_id, c.source_result_id, ch.depth+1
                             FROM curiosities c JOIN chain ch ON c.id = ch.parent
                             WHERE ch.depth < 20
                           )
                           SELECT srid FROM chain WHERE srid IS NOT NULL LIMIT 1""",
                        (_cid,)
                    ).fetchone()
                    if _prow2:
                        _source_result_id = _prow2[0]
                _pdb.close()
            except Exception:
                pass

            # Closure-lane guard: [CANDIDATE-RETEST] and [BOUNDARY] tasks are
            # closed via the body's "CURIOSITY_ID: <n>" line (1g / reconciler /
            # intake boundary-resolve) — one minted without a resolvable id can
            # never close and its curiosity re-dispatches. Skip; the next cycle
            # retries the text match. _retest_moot additionally skips retests
            # whose source already holds its credit (sibling curiosities).
            if ((_cid == "" or _retest_moot)
                    and text.lstrip().upper().startswith(("[CANDIDATE-RETEST]", "[BOUNDARY]"))):
                continue

            # Knowledge feed (option A, 2026-06-26): surface CONFIRMED prior
            # findings (not just similar hypotheses) so the next worker in this
            # domain builds on accumulated knowledge instead of starting fresh.
            # The worker stays stateless — the knowledge is read live from the
            # RAG index (prometheus.db mirror) and baked into the task body.
            # Quality filter: keep only positive-verdict findings (CONFIRMED /
            # SUPPORTED), drop REFUTED / INCONCLUSIVE / PARTIAL. Downstream
            # machinery (replication_tracker, contradiction_detector,
            # spurious_agreement -> DISPUTED) catches anything confident-but-wrong
            # that slips through; no pre-filter on confidence is needed here.
            # Ranked by RAG relevance score (clean float); worker_results.confidence
            # is a mixed text/float column, so it is deliberately NOT joined on.
            # (query_rag is ~0.12s/call with the FAISS index — feeding every task
            # costs ~6s/cycle, well under the 120s cron budget. The old "every 3rd
            # task" throttle was for the pre-FAISS 2s/query era and is now moot.)
            # suppress_prior_context (2026-07-04): epistemic lanes run BLIND —
            # a retest/boundary/clean-room card must not be handed the very
            # conclusions whose independent confirmation it exists to produce
            # (41/41 recent retest cards carried the feed before this guard).
            try:
                from prior_feed_stamp import suppress_prior_context as _suppress_fn
                _suppress_prior = _suppress_fn(text)
            except Exception:
                _suppress_prior = False
            _prior_context = ""
            if text and len(text) > 10 and not _suppress_prior:
                try:
                    sys.path.insert(0, SCRIPTS_DIR)
                    from experiment_rag import query_rag
                    # Over-fetch (top_k=6) so the verdict filter still leaves ~3.
                    # Multiprocessing timeout: skip RAG if it hangs (fixes refiller stale issue)
                    import multiprocessing as _mp
                    def _rag_worker(q, text_arg):
                        try:
                            from experiment_rag import query_rag as _qr
                            r = _qr(text_arg, top_k=6, worker_id="refiller_enrichment")
                            q.put(r)
                        except Exception:
                            q.put(None)
                    _mpq = _mp.Queue()
                    _p = _mp.Process(target=_rag_worker, args=(_mpq, text))
                    _p.start()
                    _p.join(timeout=15)
                    if _p.is_alive():
                        _p.terminate()
                        _p.join(timeout=2)
                        _rag_results = None
                    else:
                        _rag_results = _mpq.get() if not _mpq.empty() else None
                    # print(f"
                    if _rag_results and not isinstance(_rag_results, dict):
                        _parts = []
                        for _r in _rag_results:
                            _score = _r.get("score", 0)
                            if _score <= 0.5:
                                continue
                            _res = (_r.get("result") or "").strip()
                            _resU = _res.upper()
                            # Quality filter: positive verdicts only.
                            if not (_resU.startswith("CONFIRMED") or _resU.startswith("SUPPORTED")):
                                continue
                            # Exclude self-negating "CONFIRMED ... REFUTED/does NOT" text.
                            if " REFUTED" in _resU[:60] or "DOES NOT" in _resU[:60]:
                                continue
                            _finding = _res[:180] if _res else (_r.get("hypothesis") or "")[:120]
                            if _finding:
                                _parts.append(f"- [{_score:.2f}] {_finding}")
                            if len(_parts) >= 3:
                                break
                        if _parts:
                            _prior_context = (
                                "\n\nCONFIRMED PRIOR FINDINGS (build on these, do not re-test; "
                                "they have NOT all survived independent replication, so treat as "
                                "strong priors, not ground truth):\n" + "\n".join(_parts)
                            )
                except Exception:
                    pass

            body = f"""Experiment: {text}

HYPOTHESIS: {text}

METHOD:
1. Design and run a computational experiment to test this hypothesis
2. Search for relevant data/papers if needed (web_search or available datasets)
3. Analyze results quantitatively — compute effect sizes, confidence intervals, or accuracy metrics
4. Evaluate if the hypothesis is supported (seeking ≥70% threshold for confirmation)
5. If confirmed, identify the mechanism: WHY IT WORKS

PREREGISTER (MANDATORY — before writing any code): state PREDICTION: SUPPORTED
or REFUTED, a CONFIDENCE 0.0-1.0, and one WHY line — what your prior expects
this experiment to show. NEVER revise it after seeing results; report it
verbatim via --predicted-direction (see RESULT WRITING). A wrong prediction
with an honest result is MORE valuable than a correct one — it means the
experiment carried information your prior did not have.

GPU AVAILABLE: A local RTX 5090 (32GB VRAM) is available. When writing torch/ML code, use
`DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')` — do NOT hardcode 'cpu'.
For sklearn operations (LogisticRegression, PCA, StandardScaler, etc.), standard `from sklearn...`
imports are automatically GPU-accelerated via a transparent hook. For large models/inference,
use `gpu_run inference --model <model> --input data.jsonl --output results.jsonl`.

RESULT WRITING (MANDATORY — run this BEFORE kanban_complete):
  python3 ~/.hermes/scripts/write_worker_result.py --experiment <experiment_id> --finding "CONFIRMED/REFUTED: summary. WHY IT WORKS: mechanism" --supported --confidence 0.XX --domain auto --tags CONFIRMED --basis independent_computation --predicted-direction '{{"<hypothesis_key>":1}}' --files "exp_<id>.py,exp_<id>_results.json"
  (--confidence is HARD-CAPPED at 0.85 for a single result — 0.85 = high confidence, the system aggregates across workers for anything higher. Values >0.85 are REJECTED. When unsure, go lower.)

NOTE: --predicted-direction carries your PREREGISTER prediction (made BEFORE the
  experiment ran) as a single {{"<hypothesis_key>": +1|-1}} — +1 if your prior
  expects SUPPORTED, -1 if REFUTED. Reuse the SAME key in --observed-direction
  with the measured sign. Report it even, especially, when it turned out wrong.
NOTE: The '--domain auto' flag will auto-classify the domain. If you know the domain, pass --domain <domain_name> explicitly.
NOTE: Always include the WHY IT WORKS mechanism explanation — findings without mechanism are not citable.
NOTE: --basis is MANDATORY and states what your verdict RESTS ON:
  independent_computation = you ran code/math here that tests the claim itself
  replication             = you reproduced a prior result with your own run
  simulation              = synthetic/simulated data constructed for this test
  literature_authority    = you trust a paper/source WITHOUT independently testing it
  (literature_authority confidence is auto-capped at 0.5 — citing a peer-reviewed paper is a
  report, not a verification. Honest caveats like "cannot independently replicate" are never
  penalized; they just cap confidence at 0.6.)
NOTE: Save your experiment code as a .py FILE and run the file (not inline snippets), then list
  it in --files — listed files are preserved to ~/.hermes/artifacts/ so the experiment stays
  replicable after your workspace is removed.

CURIOSITY_ID: {_cid}{_prior_context}"""
            tid = "t_" + uuid.uuid4().hex[:8]
            _priority = get_task_priority(item, lane)
            try:
                conn.execute(
                    "INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, goal_mode, goal_max_turns, skills) VALUES (?, ?, ?, ?, 'ready', ?, ?, 1, 6, '[\"kanban-worker\"]')",
                    (tid, title, body, next_worker, _priority, now)
                )
                # Durable prior-fed stamp (independence gate): record whether this task
                # carried the CONFIRMED PRIOR FINDINGS feed, keyed by task id in
                # prometheus.db so it survives kanban body archival. Best-effort.
                try:
                    from prior_feed_stamp import record as _stamp_prior_feed
                    _stamp_prior_feed(tid, body)
                except Exception:
                    pass
                existing.add(title[:80])
                worker_counts[next_worker] = worker_counts.get(next_worker, 0) + 1
                # Round-robin to next least-loaded worker
                next_worker = min(workers, key=lambda w: worker_counts.get(w, 0))
                created += 1
                # Track which creations were [refiller] for the summary (2026-06-23 floor)
                if _title_prefix == "[REFILLER] ":
                    refiller_created += 1
                # Transfer tracking: link task to transfer_tracking row
                if TRANSFER_TRACKING_AVAILABLE and lane == "transfer":
                    target_domain = item.get("thread", "unknown") if isinstance(item, dict) else "unknown"
                    try:
                        linked = False
                        if _source_result_id:
                            linked = update_task_created(int(_source_result_id), target_domain, tid)
                        if not linked:
                            # Fallback (2026-06-24): source_result_id was NULL on the
                            # curiosity (synthesis-spawned). Match the tracking row by
                            # source_domain (parsed from [TRANSFER from X]) + target_domain.
                            _src_dom = parse_transfer_target(text)
                            if _src_dom:
                                linked = update_task_created_by_domain(_src_dom, target_domain, tid)
                    except Exception as _tt_err:
                        # Best-effort; don't block task creation. Log to stderr so the
                        # next transfer-tracking misdiagnosis doesn't start from a silent
                        # call-site failure (this block is the documented "functions
                        # correct, never called" recurrence point — see architecture
                        # changelog 2026-06-24).
                        print(
                            f"[transfer_tracking] link failed for task {tid} "
                            f"(srid={_source_result_id}, lane={lane}): {_tt_err}",
                            file=sys.stderr, flush=True,
                        )
            except Exception:
                continue
        conn.commit()
        conn.close()
        return created, refiller_created, f"Direct DB: created {created}/{count} {lane} tasks"
    except Exception as e:
        return 0, 0, f"Direct DB error: {e}"


def run_novelty_injection():
    """Run novelty injection scripts. Returns summary string.

    cross_domain_inject.py was REMOVED from this path — it now runs as its own
    no_agent cron (cross-domain-inject) so it fires on a fixed schedule regardless
    of whether the refiller skips (pool full). Gating it behind a non-skipped
    refiller cycle starved cross-domain transfer generation whenever the pool sat
    at target. The flock guard in cross_domain_inject.py prevents overlap.
    """
    injected = []
    for script in ("queue_entropy_monitor.py",):
        try:
            args = ["python3", os.path.join(SCRIPTS_DIR, script)]
            if script == "queue_entropy_monitor.py":
                args.append("--inject")
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=180,
                env={**os.environ, "HERMES_HOME": HERMES_HOME},
            )
            tail = (result.stdout or result.stderr or "").strip().split("\n")[-3:]
            injected.append(f"{script}: {' | '.join(t.strip() for t in tail if t.strip())}")
        except Exception as e:
            injected.append(f"{script}: ERROR {e}")
    return injected


def write_summary(summary):
    """Write summary JSON for Director to read."""
    try:
        with open(SUMMARY_PATH, "w") as f:
            json.dump(summary, f, indent=2)
    except Exception as e:
        print(f"WARNING: Failed to write summary: {e}")


def main():
    lock_fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[task_refiller] Another instance running, skipping.")
        return

    start_time = time.time()
    now = datetime.now(timezone.utc).isoformat()

    # --- Read current state ---
    ready_before = get_ready_count()
    running = get_running_count()
    entropy_before, _ = compute_entropy()

    # --- Decide how many tasks to create ---
    # When pool is empty, fill to READY_TARGET_DEPTH directly (not capped at MAX_CREATE_PER_CYCLE)
    if ready_before < READY_TARGET_DEPTH // 2:
        budget = READY_TARGET_DEPTH - ready_before
    else:
        budget = create_budget(ready_before)
    plan = lane_plan(current_ready=ready_before)
    genuine_cap = dict(plan).get("genuine", 0)
    transfer_cap = dict(plan).get("transfer", 0)

    summary = {
        "timestamp": now,
        "ready_before": ready_before,
        "running": running,
        "target_depth": READY_TARGET_DEPTH,
        "budget": budget,
        "entropy_before": entropy_before,
    }

    if budget <= 0:
        summary["action"] = "skip"
        summary["reason"] = f"Pool full ({ready_before}/{READY_TARGET_DEPTH})"
        summary["genuine_created"] = 0
        summary["transfer_created"] = 0
        summary["novelty_injected"] = False
    else:
        # --- Create tasks in one shot (rapid-fill mode handles batching internally) ---
        plan = lane_plan(count=budget)
        genuine_cap = dict(plan).get("genuine", 0)
        transfer_cap = dict(plan).get("transfer", 0)

        total_genuine, genuine_refiller, _ = run_batch_create("genuine", genuine_cap)
        total_transfer, _, _ = run_batch_create("transfer", transfer_cap)

        # Check genuine underfill for novelty injection
        novelty_injected = False
        novelty_details = []
        if genuine_cap >= 4 and total_genuine < max(2, genuine_cap // 2):
            novelty_details = run_novelty_injection()
            novelty_injected = True

        # --- Compute post-state ---
        ready_after = get_ready_count()
        entropy_after, _ = compute_entropy()
        rates = get_recent_rates()
        top_domains = get_top_domains()

        summary.update({
            "action": "refill",
            "genuine_created": total_genuine,
            "genuine_refiller": genuine_refiller,
            "transfer_created": total_transfer,
            "total_created": total_genuine + total_transfer,
            "ready_after": ready_after,
            "novelty_injected": novelty_injected,
            "novelty_details": novelty_details,
            "entropy_after": entropy_after,
            "creation_rate": rates["creation_rate"],
            "consumption_rate": rates["consumption_rate"],
            "velocity": rates["velocity"],
            "top_domains": top_domains,
            "elapsed_seconds": round(time.time() - start_time, 2),
        })

    # --- Output ---
    write_summary(summary)

    # Print summary to stdout (for cron capture)
    print(json.dumps(summary, indent=2))

    # Human-readable status line
    if summary["action"] == "skip":
        print(f"\nSKIP: Pool full ({ready_before}/{READY_TARGET_DEPTH})")
    else:
        gc = summary["genuine_created"]
        gr = summary.get("genuine_refiller", 0)
        tc = summary["transfer_created"]
        print(f"\nREFILLED: {gc} genuine ({gr} [refiller]) + {tc} transfer = {gc+tc} tasks")
        print(f"  Ready: {ready_before} -> {summary['ready_after']}")
        print(f"  Entropy: {entropy_before} -> {summary['entropy_after']}")
        print(f"  Velocity: {summary['velocity']} tasks/min")
        if novelty_injected:
            print(f"  Novelty injected: YES")

    # Release the flock (also auto-released on process exit)
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()


if __name__ == "__main__":
    main()
