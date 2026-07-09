#!/usr/bin/env python3
"""
sync_curiosity_views.py — Single source of truth sync.

Reads active curiosities from SQLite curiosities table and materializes
JSON views in self_state.json:
  - curiosity_queue (sorted by priority, capped at 400)
  - metrics.curiosities_active (count)

SQLite curiosities table is the canonical store. This script derives
JSON from it. Run every 2 minutes via cron.

Also auto-resolves items that have [RESOLVED] or [COVERED] in their text.
"""
import os
import re
import sys
import time
import json

from db_retry import get_db

HERMES_HOME = os.path.join(os.path.expanduser("~"), ".hermes")
DB_PATH = os.path.join(HERMES_HOME, "prometheus.db")
SELF_STATE_PATH = os.path.join(HERMES_HOME, "self_state.json")
MAX_QUEUE_SIZE = 200  # Reduced from 400 to prevent queue bloat
PRIORITY_MAP = {"high": 1, "medium": 3, "low": 5}


def load_state():
    try:
        with open(SELF_STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(SELF_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def auto_retire_low_score(conn):
    """Retire curiosities that are old and too low-score to ever resolve.

    Handles three categories:
    1. Active items with combined_score < threshold (original behavior)
    2. Inactive items — orphaned status no current script creates. Retire
       them the same as low-score active items so they don't accumulate.
    3. Active or inactive items with NULL combined_score that are old
       enough — if the scorer hasn't scored them in 3 days, it never will.

    Also retires any item with a corrupted status string (not one of the
    known statuses: active, inactive, resolved, retired).
    """
    c = conn.cursor()
    now = time.time()
    CUTOFF_AGE = 3 * 86400  # 3 days
    SCORE_THRESHOLD = 25    # well below the de facto ~30 selection cliff
    DEEP_FLOOR = 8          # deep lineages (evidence_depth >= 8) are exempt from
                            # age-based NULL-score retirement. During a dispatch
                            # stall a deep item can sit un-scored (combined_score
                            # NULL) past the 3-day cutoff purely because the
                            # pipeline never reached it — "un-scored" then means
                            # "un-reached", NOT "low value". Pruning those on age
                            # silently destroys good deep research. Deep items are
                            # still retired on actual low score (Rule 1), never on
                            # the NULL-after-3-days heuristic (Rule 2).
                            # narrowed_boundary curiosities are ALSO exempt from
                            # Rule 2: they carry score=NULL by construction and
                            # wait on a small reserved lane — age-retiring them
                            # killed 502 of ~1200 unrun (a NARROWED attack's
                            # boundary then never gets mapped and the claim
                            # churns attack->NARROWED). They still fall to
                            # Rule 1 on a real low score.
    KNOWN_STATUSES = ('active', 'inactive', 'resolved', 'retired')

    total_retired = 0

    # --- 1. Low-score active or inactive items ---
    c.execute("""
        SELECT COUNT(*) FROM curiosities
        WHERE status IN ('active', 'inactive')
        AND combined_score IS NOT NULL
        AND combined_score < ?
        AND created_at < ?
    """, (SCORE_THRESHOLD, now - CUTOFF_AGE))
    count = c.fetchone()[0]
    if count > 0:
        c.execute("""
            UPDATE curiosities
            SET status='retired'
            WHERE status IN ('active', 'inactive')
            AND combined_score IS NOT NULL
            AND combined_score < ?
            AND created_at < ?
        """, (SCORE_THRESHOLD, now - CUTOFF_AGE))
        conn.commit()
        total_retired += count

    # --- 2. NULL-score items that are old enough — scorer never reached them ---
    # Deep lineages (evidence_depth >= DEEP_FLOOR) are exempt: a NULL score on a
    # deep item that aged past the cutoff almost always means the dispatch
    # pipeline never reached it (a stall), not that it's worthless. Let those be
    # pruned by Rule 1 on real low score instead, never killed on age alone.
    c.execute("""
        SELECT COUNT(*) FROM curiosities
        WHERE status IN ('active', 'inactive')
        AND combined_score IS NULL
        AND created_at < ?
        AND COALESCE(evidence_depth, 0) < ?
        AND COALESCE(provenance, '') != 'narrowed_boundary'
    """, (now - CUTOFF_AGE, DEEP_FLOOR))
    count = c.fetchone()[0]
    if count > 0:
        c.execute("""
            UPDATE curiosities
            SET status='retired'
            WHERE status IN ('active', 'inactive')
            AND combined_score IS NULL
            AND created_at < ?
            AND COALESCE(evidence_depth, 0) < ?
            AND COALESCE(provenance, '') != 'narrowed_boundary'
        """, (now - CUTOFF_AGE, DEEP_FLOOR))
        conn.commit()
        total_retired += count

    # --- 3. All remaining 'inactive' items older than cutoff ---
    # 'inactive' is an orphaned status — no current script creates or manages
    # it. Items with this status are not in the working queue (which only
    # selects status='active'), are not being scored, and will never be
    # reactivated. Retire them regardless of score so they don't accumulate.
    c.execute("""
        SELECT COUNT(*) FROM curiosities
        WHERE status='inactive'
        AND created_at < ?
    """, (now - CUTOFF_AGE,))
    count = c.fetchone()[0]
    if count > 0:
        c.execute("""
            UPDATE curiosities
            SET status='retired'
            WHERE status='inactive'
            AND created_at < ?
        """, (now - CUTOFF_AGE,))
        conn.commit()
        total_retired += count

    # --- 4. Corrupted status strings (not a known status) ---
    c.execute("""
        SELECT COUNT(*) FROM curiosities
        WHERE status NOT IN ('active', 'inactive', 'resolved', 'retired')
    """)
    count = c.fetchone()[0]
    if count > 0:
        c.execute("""
            UPDATE curiosities
            SET status='retired'
            WHERE status NOT IN ('active', 'inactive', 'resolved', 'retired')
        """)
        conn.commit()
        total_retired += count

    return total_retired


def auto_resolve_stale(conn):
    """Mark active items as resolved if their text contains [RESOLVED] or [COVERED]."""
    c = conn.cursor()
    now = time.time()
    
    c.execute("""SELECT id, text FROM curiosities 
                 WHERE status='active' 
                 AND (text LIKE '%RESOLVED%' OR text LIKE '%COVERED%')""")
    stale = c.fetchall()
    
    updated = 0
    for cur_id, text in stale:
        match = re.search(r'(exp_\d+[a-z]?\b)', text)
        resolved_by = match.group(1) if match else None
        c.execute("""UPDATE curiosities 
                     SET status='resolved', resolved_by_experiment=?, resolved_at=?
                     WHERE id=?""", (resolved_by, now, cur_id))
        updated += 1
    
    if updated:
        conn.commit()
    return updated


def get_hidden_bridge_pairs():
    """Load hidden bridge pairs from routing outcome cache."""
    try:
        with open(os.path.join(HERMES_HOME, "hidden_bridge_pairs.json")) as f:
            pairs = json.load(f)
            return set(tuple(p) if isinstance(p, list) else p for p in pairs)
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def load_routing_scores():
    """Load routing scores keyed by (source, target) from routing_outcome_cache.json.
    
    Returns a dict mapping (source_str, target_str) → edge dict with routing_score,
    success_rate, flow_norm, etc.  Both source and target use underscore-delimited
    machine names (matching the DB 'source'/'target' columns from the edge cache).
    """
    try:
        cache_path = os.path.join(HERMES_HOME, "routing_outcome_cache.json")
        with open(cache_path) as f:
            cache = json.load(f)
        edges = cache.get("edges", cache) if isinstance(cache, dict) else cache
        if isinstance(edges, dict):
            edges = list(edges.values())
        lookup = {}
        for e in (edges if isinstance(edges, list) else []):
            src = (e.get("source") or "").strip().lower()
            tgt = (e.get("target") or "").strip().lower()
            if src and tgt:
                lookup[(src, tgt)] = e
        return lookup
    except Exception:
        return {}


def is_hidden_bridge(text, hidden_pairs):
    """Check if a curiosity text represents a hidden bridge."""
    if not text or "[TRANSFER from" not in text:
        return False
    try:
        parts = text.split("] Does ")
        if len(parts) != 2:
            return False
        src = parts[0].replace("[TRANSFER from ", "").replace("_", " ").lower()
        tgt = parts[1].split(" transfer to ")[1].split("?")[0].strip().lower()
        return (src, tgt) in hidden_pairs or (src.replace(" ", "_"), tgt.replace(" ", "_")) in hidden_pairs
    except (IndexError, ValueError):
        return False


def get_active_curiosities(conn):
    """Fetch all active curiosities sorted by priority, with hidden bridge boost."""
    hidden_pairs = get_hidden_bridge_pairs()
    c = conn.cursor()
    c.execute("""SELECT id, text, priority, source_experiment, created_at, evidence_depth
                 FROM curiosities 
                 WHERE status='active'
                 ORDER BY priority ASC, created_at DESC""")
    rows = c.fetchall()
    
    items = []
    hidden_items = []
    for row in rows:
        cur_id, text, priority, source_exp, created_at, evidence_depth = row
        # Convert numeric priority back to label for JSON compatibility
        try:
            pri_num = int(priority) if priority is not None else 3
        except (ValueError, TypeError):
            pri_num = PRIORITY_MAP.get(str(priority).lower(), 3) if priority else 3
        
        if pri_num <= 1:
            pri_label = "high"
        elif pri_num <= 3:
            pri_label = "medium"
        else:
            pri_label = "low"
        
        item = {
            "text": text,
            "source": source_exp or "unknown",
            "priority": pri_label,
            "db_id": cur_id,
        }
        
        # Hidden bridge boost: separate hidden bridges for prioritization
        if hidden_pairs and is_hidden_bridge(text, hidden_pairs):
            item["priority"] = "high"  # Force high priority for hidden bridges
            hidden_items.append(item)
        # Deep lineage: tag for later guaranteed-slice allocation.
        # FIX (2026-06-23): tag ANY deep curiosity (evidence_depth >= 10), not just
        # source='synthesis'. The depth-climb produces deep follow-ups sourced from
        # real worker experiments (exp_*), not synthesis. Restricting the deep slice
        # to synthesis-only meant a freshly-spawned depth-19 child from a worker
        # experiment got NO dispatch priority and stalled at the next level — the
        # exact cap the dispatch fix was meant to remove. Any deep node feeds the
        # climb; all of them get the guaranteed front-of-queue slice.
        if evidence_depth and evidence_depth >= 10:
            item["synthesis_deep"] = True
        items.append(item)
    
    # Hidden bridges go first, then regular items
    return hidden_items + items


def sync():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = get_db(DB_PATH)

    try:
        # ── DB-only operations (no self_state.json access) ──

        # Step 1: Retire low-score curiosities that will never resolve
        retired = auto_retire_low_score(conn)
        if retired:
            print(f"Retired {retired} curiosities (low-score/inactive/null-score/corrupted, age > 3d)")

        # Step 2: Auto-resolve stale items
        resolved = auto_resolve_stale(conn)
        if resolved:
            print(f"Auto-resolved {resolved} stale items")

        # Step 3: Fetch active curiosities
        items = get_active_curiosities(conn)

        # Step 3.5: Dedup against existing kanban tasks
        # Remove curiosities whose text already exists as a task title.
        # This prevents the refiller from seeing 100% duplicates and creating 0 tasks.
        # EXCEPT: do NOT dedup injection-sourced items (opportunity_injection,
        # refutation_boost, moderate_success, hidden_bridge_injection). These
        # items are injected deliberately into the queue and their presence in
        # kanban reflects previous resolutions — deduping them starves the queue
        # and causes a self-perpetuating duplicate-injection cycle.
        try:
            kanban_db = os.path.join(HERMES_HOME, "kanban.db")
            if os.path.exists(kanban_db):
                kconn = get_db(kanban_db)
                krows = kconn.execute(
                                "SELECT DISTINCT substr(title, 1, 80) FROM tasks "
                                "WHERE status IN ('ready', 'running')"
                            ).fetchall()
                existing_titles = {r[0] for r in krows}
                kconn.close()
                before_dedup = len(items)
                INJECTION_SOURCES = ('opportunity_injection', 'refutation_boost',
                                     'moderate_success', 'hidden_bridge_injection',
                                     'opportunity_injection_70')
                items = [
                    i for i in items
                    if (i.get("source", "") in INJECTION_SOURCES
                        or i.get("text", "")[:80] not in existing_titles)
                ]
                deduped = before_dedup - len(items)
                if deduped:
                    print(f"  Dedup: removed {deduped} items already in kanban ({len(items)} remaining; injection items preserved)")

            # Step 3.55: Cap injection-sourced items to prevent queue starvation.
            # Injection items are exempt from kanban dedup by design, but without a
            # cap they fill all 200 queue slots with stale entries, blocking fresh
            # synthesis curiosities (priority=3) from entering the queue.
            # Verified June 21 2026: 722 stale priority=1 injection items blocked
            # 400 fresh non-duplicate synthesis items from entering the queue.
            MAX_INJECTION_FRACTION = 0.65
            max_injection = int(MAX_QUEUE_SIZE * MAX_INJECTION_FRACTION)
            injection = [i for i in items if i.get("source", "") in INJECTION_SOURCES]
            non_injection = [i for i in items if i.get("source", "") not in INJECTION_SOURCES]
            if len(injection) > max_injection:
                # Reserve a guaranteed slice of the injection budget for proven
                # partial-success bridges so the opportunity_injection flood
                # (hundreds of active items) cannot starve them — but cap that
                # reserve so protected items cannot starve the high-success
                # opportunity_injection edges either. Balanced split:
                #   - protected reserve: up to ~50% of injection budget
                #   - opportunity_injection (flood): the remainder (>=50%)
                # (Tier 3/4 starvation v3 fix — root cause was here, not the transfer cap.)
                PROTECTED_INJECTION = ("moderate_success", "refutation_boost",
                                       "hidden_bridge_injection", "opportunity_injection_70")
                protected_reserve = int(max_injection * 0.50)
                protected = [i for i in injection if i.get("source", "") in PROTECTED_INJECTION]
                flood = [i for i in injection if i.get("source", "") not in PROTECTED_INJECTION]
                # Sort protected items by routing_score (desc) so the highest-value
                # hidden bridges survive the cap instead of just the newest ones.
                # Chronological sort (created_at DESC from DB query) drops old
                # high-SR hidden bridges that have the most evidential support.
                try:
                    _routing = load_routing_scores()
                    def _routing_key(item):
                        text = (item.get("text") or "").lower()
                        if "[transfer from" not in text or "transfer to " not in text:
                            return 0.0
                        try:
                            s = text.split("[transfer from ")[1].split("]")[0].strip()
                            s = s.replace("_", " ").replace("-", " ").replace(" ", "_")
                            t = text.split("transfer to ")[1].split("?")[0].strip()
                            t = t.replace("_", " ").replace("-", " ").replace(" ", "_")
                            e = _routing.get((s, t))
                            return e.get("routing_score", 0.0) if e else 0.0
                        except Exception:
                            return 0.0
                    protected.sort(key=_routing_key, reverse=True)
                except Exception:
                    pass  # If routing cache unavailable, keep DB order
                protected = protected[:protected_reserve]
                flood_budget = max(0, max_injection - len(protected))
                flood = flood[:flood_budget]
                censored = len(injection) - (len(protected) + len(flood))
                injection = protected + flood
                items = injection + non_injection
                print(f"  Injection cap: trimmed {censored} items (max {max_injection}; "
                      f"{len(protected)} protected kept, {len(flood)} flood), {len(items)} remaining")
        except Exception as e:
            print(f"  Dedup warning: {e}")

        # Step 3.6: Intra-queue dedup — remove duplicate (source, target) pairs
        # among transfer items. The DB accumulates duplicates because inject
        # deduplicates against self_state.json, but items trimmed by the transfer
        # cap fall out of the queue (while staying active in DB), causing
        # reinjection on the next run. This dedup keeps only the first (highest
        # priority) occurrence of each pair in the queue.
        before_intra_dedup = len(items)
        seen_pairs = set()
        deduped_items = []
        for item in items:
            text = item.get("text", "")
            if "[TRANSFER" in text.upper():
                m = re.match(r'\[TRANSFER from ([^\]]+)\]', text)
                if m:
                    src = m.group(1).replace('_', ' ').lower()
                    tgt_match = re.search(r'transfer to ([^?]+)\?', text, re.IGNORECASE)
                    if tgt_match:
                        tgt = tgt_match.group(1).strip().rstrip('?').replace('_', ' ').lower()
                        pair = (src, tgt)
                        if pair in seen_pairs:
                            continue
                        seen_pairs.add(pair)
            deduped_items.append(item)
        items = deduped_items
        intra_deduped = before_intra_dedup - len(items)
        if intra_deduped:
            print(f"  Intra-queue dedup: removed {intra_deduped} duplicate pairs ({len(seen_pairs)} unique pairs, {len(items)} items)")

        # Step 4: Cap at MAX_QUEUE_SIZE with transfer diversity guarantee
        # Ensure no more than 70% of the queue is transfers — this prevents
        # synthesis transfer flood from starving genuine exploration items.
        # Carve out synthesis deep items BEFORE the MAX_QUEUE_SIZE truncation
        # so they survive the cut.
        _pre_trunc_synthesis = [i for i in items if i.get("synthesis_deep")][:20]
        if _pre_trunc_synthesis:
            _pre_trunc_ids = {id(i) for i in _pre_trunc_synthesis}
            items = [i for i in items if id(i) not in _pre_trunc_ids]
        # Same carve-out for candidate retests: they are priority 2 and sort
        # behind every priority-1 exploration item, so a deep active pool can
        # push all of them past the MAX_QUEUE_SIZE cut.
        _pre_trunc_retest = [i for i in items
                             if i.get("text", "").lstrip().upper().startswith("[CANDIDATE-RETEST]")][:32]
        if _pre_trunc_retest:
            _pre_trunc_rt_ids = {id(i) for i in _pre_trunc_retest}
            items = [i for i in items if id(i) not in _pre_trunc_rt_ids]
        _pre_trunc_boundary = [i for i in items
                               if i.get("text", "").lstrip().upper().startswith("[BOUNDARY]")][:16]
        if _pre_trunc_boundary:
            _pre_trunc_bd_ids = {id(i) for i in _pre_trunc_boundary}
            items = [i for i in items if id(i) not in _pre_trunc_bd_ids]
        if len(items) > MAX_QUEUE_SIZE:
            items = items[:MAX_QUEUE_SIZE]
        if _pre_trunc_synthesis:
            items = _pre_trunc_synthesis + items
            print(f"  Synthesis deep pre-trunc: {len(_pre_trunc_synthesis)} items reserved before MAX_QUEUE_SIZE cut")
        if _pre_trunc_retest:
            items = _pre_trunc_retest + items
            print(f"  Candidate-retest pre-trunc: {len(_pre_trunc_retest)} items reserved before MAX_QUEUE_SIZE cut")
        if _pre_trunc_boundary:
            items = _pre_trunc_boundary + items
            print(f"  Boundary pre-trunc: {len(_pre_trunc_boundary)} items reserved before MAX_QUEUE_SIZE cut")
        # Enforce diversity: split transfers and non-transfers, interleave
        # to guarantee at most 70% transfers in the final queue.
        TRANSFER_CAP_PCT = 0.70  # Allow 70% transfers to ensure hidden bridges get through
        effective_size = min(len(items), MAX_QUEUE_SIZE)
        max_transfers = int(effective_size * TRANSFER_CAP_PCT)
        non_transfer = [i for i in items if "[TRANSFER" not in i.get("text", "").upper()]
        transfer = [i for i in items if "[TRANSFER" in i.get("text", "").upper()]
        # Reserve synthesis deep items BEFORE the transfer cap trims non_transfer.
        _synthesis_deep_reserved = [i for i in non_transfer if i.get("synthesis_deep")][:20]
        if _synthesis_deep_reserved:
            _reserved_ids = {id(i) for i in _synthesis_deep_reserved}
            non_transfer = [i for i in non_transfer if id(i) not in _reserved_ids]
            print(f"  Synthesis deep reserved: {len(_synthesis_deep_reserved)} items (guaranteed slice, cap 20)")
        # Reserve a candidate-retest slice ahead of the non_transfer cut, and
        # place it at the queue head below. Retest items otherwise land
        # mid-queue (observed: positions 52-111) and the refiller's front-first
        # deficit budget never reaches them — 0 retest dispatches in 24h while
        # ~300 retest curiosities/day were injected. Same starvation mode (and
        # same fix) as the synthesis-deep slice above.
        _retest_reserved = [i for i in non_transfer
                            if i.get("text", "").lstrip().upper().startswith("[CANDIDATE-RETEST]")][:32]
        if _retest_reserved:
            _retest_ids = {id(i) for i in _retest_reserved}
            non_transfer = [i for i in non_transfer if id(i) not in _retest_ids]
            print(f"  Candidate-retest reserved: {len(_retest_reserved)} items (guaranteed slice, cap 32; drain mode 2026-07-02)")
        # Boundary-mapping slice ([BOUNDARY], provenance narrowed_boundary):
        # scores ~26-29 never win queue positions against the mass lanes —
        # 0 dispatched ever without a reservation. Placed behind retests;
        # the refiller's [BOUNDARY] sub-pool (target 2) does the minting.
        _boundary_reserved = [i for i in non_transfer
                              if i.get("text", "").lstrip().upper().startswith("[BOUNDARY]")][:16]
        if _boundary_reserved:
            _boundary_ids = {id(i) for i in _boundary_reserved}
            non_transfer = [i for i in non_transfer if id(i) not in _boundary_ids]
            print(f"  Boundary reserved: {len(_boundary_reserved)} items (guaranteed slice, cap 16; drain mode 2026-07-04)")
        non_transfer = non_transfer[:MAX_QUEUE_SIZE - max_transfers]
        # NOTE: do NOT pre-trim `transfer` to max_transfers here. opportunity_injection
        # items (591 active) vastly outnumber moderate_success (30) and refutation_boost
        # (68); since all share priority=1 and sort by created_at DESC, a blanket
        # transfer[:max_transfers] slice drops nearly all moderate/refutation items
        # before the protection logic below runs. We bucket FIRST, then cap each bucket.
        #
        # Hidden bridges: proven partial-success or refutation edges that need
        # priority placement.  Use source field (not priority+text pattern match,
        # which catches ALL injection items and breaks ratio enforcement).
        HIDDEN_BRIDGE_SOURCES = ("moderate_success", "refutation_boost",
                                  "hidden_bridge_injection")
        hidden_transfers = [i for i in transfer if i.get("source", "") in HIDDEN_BRIDGE_SOURCES]
        rest_transfers = [i for i in transfer if i not in hidden_transfers]
        # Reserve guaranteed slots for proven-partial-success bridges so the
        # opportunity_injection flood cannot starve them (Tier 3/4 starvation, v3 fix).
        PROTECTED_SOURCES = ("opportunity_injection_70",)
        MIN_PROTECTED_SLOTS = max(4, max_transfers // 8)  # ~12% of transfer budget reserved
        protected_transfers = [i for i in rest_transfers if i.get("source", "") in PROTECTED_SOURCES]
        regular_transfers = [i for i in rest_transfers if i.get("source", "") not in PROTECTED_SOURCES]
        # Cap protected slice at its reserve; cap regulars with whatever's left.
        protected_transfers = protected_transfers[:MIN_PROTECTED_SLOTS]
        regular_transfer_cap = max(0, max_transfers - len(hidden_transfers) - len(protected_transfers))
        regular_transfers = regular_transfers[:regular_transfer_cap]
        # FIX (2026-06-23): Do NOT prepend synthesis deep items to non_transfer.
        # non_transfer sits THIRD in the final queue order (after hidden_transfers
        # and protected_transfers, ~35 items), so deep items landed at position ~39.
        # task_refiller dispatches ~6 genuine slots/cycle from the FRONT and never
        # reached position 39 — every deep item aged out at the 3-day retire instead
        # of being dispatched (7,690 deep tasks archived, depth-climb stalled at d17).
        # Deep items are the guaranteed slice; they must dispatch FIRST. Place them
        # at the absolute head of the queue, ahead of hidden bridges.

        # Final order: synthesis deep (guaranteed, dispatched first) → candidate
        # retests (guaranteed slice) → hidden bridges → protected partial-success
        # bridges → non-transfer → regular transfers
        items = _synthesis_deep_reserved + _retest_reserved + _boundary_reserved + hidden_transfers + protected_transfers + non_transfer + regular_transfers
        transfer_actual = len(hidden_transfers) + len(protected_transfers) + len(regular_transfers)
        pct = 100 * transfer_actual / max(len(items), 1)
        
        # Post-assembly: enforce transfer ratio cap.
        # When the queue is below MAX_QUEUE_SIZE, the absolute max_transfers
        # (based on MAX_QUEUE_SIZE) can produce ratios > 70%.  Trim from
        # lowest-priority transfers first (regular → protected → hidden).
        # Algebra: target_N/(target_N + non_N) = 0.70 → target_N = ⌈non_N × (0.70/0.30)⌉
        target_pct = transfer_actual / max(len(items), 1)
        if target_pct > TRANSFER_CAP_PCT:
            non_transfer_count = len(non_transfer)
            # Solve: transfers ≤ non_transfer × (cap / (1-cap))
            # When non_transfer_count = 0, use effective=1 so ratio enforcement
            # keeps at least some items instead of emptying the entire queue.
            effective_non_transfer = max(non_transfer_count, 1)
            max_allowed_transfers = int(effective_non_transfer * (TRANSFER_CAP_PCT / (1 - TRANSFER_CAP_PCT)))
            # Trim in priority order: regular first, then protected, then hidden
            surplus = transfer_actual - max_allowed_transfers
            if surplus > 0:
                trim_from_regular = min(surplus, len(regular_transfers))
                regular_transfers = regular_transfers[:max(0, len(regular_transfers) - trim_from_regular)]
                surplus -= trim_from_regular
            if surplus > 0:
                trim_from_protected = min(surplus, len(protected_transfers))
                protected_transfers = protected_transfers[:max(0, len(protected_transfers) - trim_from_protected)]
                surplus -= trim_from_protected
            if surplus > 0:
                hidden_transfers = hidden_transfers[:max(0, len(hidden_transfers) - surplus)]
            items = _synthesis_deep_reserved + _retest_reserved + _boundary_reserved + hidden_transfers + protected_transfers + non_transfer + regular_transfers
            transfer_actual = len(hidden_transfers) + len(protected_transfers) + len(regular_transfers)
            pct = 100 * transfer_actual / max(len(items), 1)
            print(f"  Ratio enforcement: trimmed to {transfer_actual} transfers ({pct:.0f}%) to respect {TRANSFER_CAP_PCT:.0%} cap")
        
        print(f"  Queue: {len(hidden_transfers)} hidden bridges + {len(protected_transfers)} protected + {len(non_transfer)} genuine + {len(regular_transfers)} transfer ({pct:.0f}% transfer)")

        # ── State write: use SHARED state_write_lock so apply_worker_results ──
        # ── cannot clobber our writes (they share self_state.json.lock).    ──
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from state_lock import state_write_lock

        with state_write_lock() as state:
            if state is None:
                print("[sync_curiosity_views] Could not acquire state lock, skipping.")
                return

            old_queue = state.get("curiosity_queue", [])
            old_size = len(old_queue)

            state["curiosity_queue"] = items
            state["curiosity_queue_size"] = len(items)

            # Update metrics
            if "metrics" not in state:
                state["metrics"] = {}
            state["metrics"]["curiosities_active"] = len(items)
            state["metrics"]["curiosity_queue_size"] = len(items)

            # Remove the Director's shortlist (no longer needed)
            if "curiosities_queue" in state:
                del state["curiosities_queue"]

            # Update last_updated
            state["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            # save_state() is called automatically on context manager exit

        # Step 6: Report
        print(f"Synced: {len(items)} active curiosities -> curiosity_queue")
        print(f"  Queue: {old_size} -> {len(items)}")
        print(f"  metrics.curiosities_active = {len(items)}")

        # DB stats
        c = conn.cursor()
        c.execute("SELECT status, COUNT(*) FROM curiosities GROUP BY status")
        stats = dict(c.fetchall())
        print(f"  DB: {stats.get('active', 0)} active, {stats.get('resolved', 0)} resolved")

    except Exception as e:
        print(f"[sync_curiosity_views] Error: {e}", file=sys.stderr)
        import traceback; traceback.print_exc()
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sync()
