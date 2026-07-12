from db_retry import get_db
from prometheus_paths import HERMES_HOME as _PP_HERMES_HOME
from transfer_convergence import is_pair_contested
#!/usr/bin/env python3
"""Inject high-opportunity edges into the curiosity queue (self_state.json AND prometheus.db).

Two-tier injection:
  Tier 1: SR >= 80%, attempts >= 3, low traffic (primary)
  Tier 2: SR 70-80%, attempts >= 3, low traffic (fallback when tier 1 is exhausted)

Queue-state feedback (2026-06-19 stability fix):
  Before injecting, check the current domain distribution of active curiosities
  in the queue. Skip target domains that are already saturated (>= 15 pending
  curiosities from that domain in the last 2 hours). This converts the injector
  from open-loop (blind injection) to closed-loop (state-aware injection),
  eliminating the overshoot that causes Gini oscillation. A batch cap of 50
  limits the total control action per run.
"""
import json, os, time, sys, sqlite3

HERMES = _PP_HERMES_HOME
SELF_STATE = os.path.join(HERMES, "self_state.json")
OUTCOME_CACHE = os.path.join(HERMES, "routing_outcome_cache.json")
DB_PATH = os.path.join(HERMES, "prometheus.db")

# Stability fix parameters (2026-06-19)
SATURATION_THRESHOLD = 15  # max active curiosities per domain before skipping
BATCH_CAP = 50  # max total injections per run (was unbounded / auto-tune controlled)


def _get_queue_domain_counts(conn):
    """Get domain distribution of active curiosities in the queue (last 2 hours)."""
    domain_counts = {}
    try:
        for row in conn.execute(
            "SELECT e.domain, COUNT(*) as cnt "
            "FROM curiosities c "
            "LEFT JOIN experiments e ON e.id = c.source_experiment "
            "WHERE c.status = 'active' AND c.created_at > ? "
            "GROUP BY e.domain ORDER BY cnt DESC",
            (int(time.time()) - 7200,)
        ).fetchall():
            if row[0]:
                domain_counts[row[0]] = row[1]
    except Exception:
        pass
    return domain_counts

def load_json_retry(path, retries=3, delay=1):
    """Load JSON with retry — handles race with concurrent writers."""
    for attempt in range(retries):
        try:
            with open(path) as f:
                data = json.load(f)
            if not data and attempt < retries - 1:
                time.sleep(delay)
                continue
            return data
        except (json.JSONDecodeError, ValueError):
            if attempt < retries - 1:
                time.sleep(delay)
                continue
            raise

AUTO_TUNE_STATE = os.path.join(HERMES, "auto_tune_state.json")

def main():
    # Read injection rate from auto_tune state (auto-tune adjusts this)
    # Falls back to --count CLI arg, then to default of 5
    count = 11  # default fallback
    try:
        with open(AUTO_TUNE_STATE) as f:
            tune_state = json.load(f)
        count = tune_state.get("injection_rate", 5)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if "--count" in sys.argv:
        idx = sys.argv.index("--count")
        count = int(sys.argv[idx + 1])

    cache = load_json_retry(OUTCOME_CACHE)
    state = load_json_retry(SELF_STATE)

    # Queue-state feedback (2026-06-19 stability fix):
    # Load current domain distribution from DB so we can skip saturated domains.
    queue_domain_counts = {}
    try:
        _conn = get_db(DB_PATH)
        queue_domain_counts = _get_queue_domain_counts(_conn)
        _conn.close()
    except Exception:
        pass

    # Apply batch cap: min(requested count, BATCH_CAP)
    effective_cap = min(count, BATCH_CAP)

    # Per-tier minimum quotas (2026-06-20): Without quotas, Tiers 1+2 consume
    # all slots at injection_rate=15, starving Tier 3 (moderate SR 50-70%)
    # and Tier 4 (refutation SR<50%). Minimum quotas guarantee each tier
    # gets slots regardless of how many high-SR candidates exist.
    MIN_TIER2_SLOTS = max(1, effective_cap // 7)   # at least 14% for 70-80% hidden bridges
    MIN_TIER3_SLOTS = max(1, effective_cap // 5)   # at least 20% for moderate hidden bridges
    MIN_TIER4_SLOTS = max(1, effective_cap // 10)  # at least 10% for refutation exploration

    queue = state.get("curiosity_queue", [])
    # Dedup by (source_human, target_human) pair, not by text prefix.
    # The old text[:100] approach caused false positives because transfer
    # items share a common prefix format and long source domain names
    # pushed the target domain past the 100-char cutoff.
    # Fix: also handle [COMPRESSION] items with embedded [TRANSFER from ...]
    # and varying transfer item formats (transfer to, apply to, etc.)
    import re as _re
    existing_pairs = set()
    for item in queue:
        text = item if isinstance(item, str) else item.get("text", "")
        # Try to extract (source, target) from [TRANSFER from source] format
        pair = None
        m = _re.match(r'\[TRANSFER from ([^\]]+)\]', text)
        if m:
            src = m.group(1).replace('_', ' ').lower()
            # Try multiple target patterns: transfer to, apply to
            for pattern in [r'transfer to ([^?]+)\?', r'apply to ([^?—]+)', r'apply to ([^?]+)\?']:
                m2 = _re.search(pattern, text)
                if m2:
                    tgt = m2.group(1).strip().rstrip('?').replace('_', ' ').lower()
                    pair = (src, tgt)
                    break
        if pair:
            existing_pairs.add(pair)
        else:
            existing_pairs.add(("_text_", text[:100].lower()))

    # Traffic filter: use attempts (actual routing activity) instead of flow_raw
    # (static topology weight that doesn't reflect current routing).
    # Threshold: 10 attempts = well-explored edges get filtered out.
    # FIX (2026-06-20): ATTEMPT_THRESHOLD was blocking proven hidden bridges.
    # Edges with 12 attempts, 100% SR, and zero flow are our MOST VALUABLE
    # undiscovered routes — they have the most confirming data, yet the
    # threshold excluded them. Now, edges with zero flow (flow_norm=0)
    # are exempt from the threshold regardless of attempt count.
    ATTEMPT_THRESHOLD = 10
    ZERO_FLOW_THRESHOLD_EXEMPT = True  # allow zero-flow edges through threshold

    # Compute max flow_norm for percentage-based hidden bridge detection.
    # The absolute 0.001 check was too strict — edges with flow_norm=0.0174
    # (1.9% of max) were blocked even though they're hidden bridges (<5% flow).
    max_flow_norm = max(
        (d.get("flow_norm", 0) for d in cache.values() if isinstance(d, dict)),
        default=1.0
    )
    HIDDEN_BRIDGE_FLOW_PCT = 0.05  # match calibration_test hidden bridge definition

    injected = 0
    new_items = []

    # --- Tier 1: SR >= 80% ---
    # Sort candidates by routing_score DESC, then by attempts ASC (hidden bridges first)
    # This ensures hidden bridges (3-4 attempts, zero flow) get injected before
    # well-explored edges (5+ attempts). Without sorting, injection picks the first
    # 15 matching items in insertion order, which misses hidden bridges entirely.
    tier1_candidates = []
    for key, data in cache.items():
        parts = key.split("->")
        if len(parts) != 2:
            continue
        src, tgt = parts
        sr = data.get("success_rate", 0)
        attempts = data.get("attempts", 0)
        flow = data.get("flow_raw", 0)
        routing_score = data.get("routing_score", 0)

        if sr < 0.80 or attempts < 3:
            continue
        # CONTESTED-PAIR GATE (Option 1): skip ill-posed / non-converging pairs
        # (high N, near-coin-flip verdict spread) — re-running them is noise.
        if is_pair_contested(src, tgt):
            continue
        flow_norm = data.get("flow_norm", 0)
        if attempts > ATTEMPT_THRESHOLD and flow_norm / max_flow_norm >= HIDDEN_BRIDGE_FLOW_PCT:
            continue  # well-explored: skip unless hidden bridge (<5% flow)

        text = f"[TRANSFER from {src}] Does {src.replace('_', ' ')} transfer to {tgt.replace('_', ' ')}? ({sr:.0%} success, {attempts} transfers)"

        src_h = src.replace('_', ' ').lower()
        tgt_h = tgt.replace('_', ' ').lower()
        if (src_h, tgt_h) in existing_pairs:
            continue

        # Sort priority: zero-flow edges (hidden bridges) first regardless of
        # attempt count, then by routing_score descending. The old `is_hidden =
        # attempts < 5` logic pushed edges with 5+ attempts and zero flow
        # (like soil_science->climate at 12 attempts, 100% SR, 0 flow) to the
        # BOTTOM of Tier 1, below 3-attempt edges with high traffic. This
        # starved the highest-value hidden bridges of injection for months.
        flow_pct = flow_norm / max_flow_norm if max_flow_norm > 0 else 0
        is_zero_flow = flow_pct < 0.01
        tier1_candidates.append((0 if is_zero_flow else 1000, -routing_score, -attempts, key, sr, attempts, flow, routing_score, text, src, tgt))

    tier1_candidates.sort()
    for _, _, _, key, sr, attempts, flow, routing_score, text, src, tgt in tier1_candidates[:effective_cap]:
        if injected >= effective_cap:
            break
        # Reserve slots for lower tiers (2026-06-20): stop Tier 1 when
        # remaining slots are needed for Tier 2+3+4 minimum quotas.
        if effective_cap - injected <= MIN_TIER2_SLOTS + MIN_TIER3_SLOTS + MIN_TIER4_SLOTS:
            break
        # Queue-state feedback: skip if target domain is saturated
        if queue_domain_counts.get(tgt, 0) >= SATURATION_THRESHOLD:
            continue
        item = {"text": text, "source_domain": src, "target_domain": tgt,
                "source": "opportunity_injection", "injected_at": time.time(),
                "routing_score": routing_score, "success_rate": sr}
        new_items.append(item)
        queue.append(item)
        src_h = src.replace('_', ' ').lower()
        tgt_h = tgt.replace('_', ' ').lower()
        existing_pairs.add((src_h, tgt_h))
        # Update count so subsequent items see the update
        queue_domain_counts[tgt] = queue_domain_counts.get(tgt, 0) + 1
        injected += 1

    # --- Tier 2: SR 70-80% (fallback when tier 1 is exhausted) ---
    phase2_count = 0
    if injected < effective_cap:
        remaining = effective_cap - injected
        candidates = []
        for key, data in cache.items():
            parts = key.split("->")
            if len(parts) != 2:
                continue
            if is_pair_contested(parts[0], parts[1]):
                continue  # CONTESTED-PAIR GATE (Option 1)
            sr = data.get("success_rate", 0)
            attempts = data.get("attempts", 0)
            flow = data.get("flow_raw", 0)
            routing_score = data.get("routing_score", 0)
            flow_norm = data.get("flow_norm", 0)

            if (0.70 <= sr < 0.80 and attempts >= 3
                    and (attempts <= ATTEMPT_THRESHOLD or flow_norm / max_flow_norm < HIDDEN_BRIDGE_FLOW_PCT)):
                text = f"[TRANSFER from {parts[0]}] Does {parts[0].replace('_', ' ')} transfer to {parts[1].replace('_', ' ')}? ({sr:.0%} success, {attempts} transfers)"
                src_h = parts[0].replace('_', ' ').lower()
                tgt_h = parts[1].replace('_', ' ').lower()
                if (src_h, tgt_h) not in existing_pairs:
                    # Queue-state feedback: skip saturated target domains
                    if queue_domain_counts.get(parts[1], 0) >= SATURATION_THRESHOLD:
                        continue
                    candidates.append((key, sr, attempts, flow, routing_score, text, parts[0], parts[1]))

        candidates.sort(key=lambda x: x[4], reverse=True)
        for key, sr, attempts, flow, routing_score, text, src, tgt in candidates[:remaining]:
            # Reserve slots for lower tiers (2026-06-20): stop Tier 2 when
            # remaining slots are needed for Tier 3+4 minimum quotas.
            # FIX (2026-06-20 v4): Use < instead of <= so Tier 2 gets at least
            # 1 slot when remaining == MIN_T3+MIN_T4 (the exact reserve amount).
            # Without this, Tier 1 alone consumed all 11 non-reserved slots and
            # Tier 2's check fired immediately (4 <= 4), starving Tier 2 entirely.
            # Tradeoff: Tier 3 minimum drops from 3 to 2 when Tier 2 needs 1 slot.
            if effective_cap - injected < MIN_TIER3_SLOTS + MIN_TIER4_SLOTS:
                break
            item = {"text": text, "source_domain": src, "target_domain": tgt,
                    "source": "opportunity_injection_70", "injected_at": time.time(),
                    "routing_score": routing_score, "success_rate": sr}
            new_items.append(item)
            queue.append(item)
            existing_pairs.add((parts[0].replace('_', ' ').lower(), parts[1].replace('_', ' ').lower()))
            queue_domain_counts[tgt] = queue_domain_counts.get(tgt, 0) + 1
            injected += 1
            phase2_count += 1

    # --- Tier 3: Moderate-success (SR 50-70%, attempts >= 3) ---
    # (formerly Tier 4 — moved before refutation to prioritize moderate hidden bridges)
    # Gap-filler (2026-06-20): Tiers 1-2 cover SR>=80% and 70-80%.
    # The 50-70% range was uncovered, leaving 25+ moderate-success edges
    # (many with flow=0, i.e. hidden bridges) invisible to the injector.
    # This tier surfaces them so the system can evaluate transfers that
    # succeed roughly half the time — neither confirmed nor refuted.
    #
    # REORDERED (2026-06-20): Moved BEFORE refutation_boost so moderate hidden
    # bridges aren't starved. At injection_rate=15, Tiers 1+2 consume all 15
    # slots, leaving zero for moderate hidden bridges. Swapping priority ensures
    # high-value ambiguous edges (SR 50-70%, proven partial success) reach the
    # queue before low-value refutation edges (SR<50%, mostly noise).
    phase3_count = 0
    if injected < effective_cap:
        remaining = effective_cap - injected
        tier3_candidates = []
        for key, data in cache.items():
            parts = key.split("->")
            if len(parts) != 2:
                continue
            src, tgt = parts
            sr = data.get("success_rate", 0)
            attempts = data.get("attempts", 0)
            flow = data.get("flow_raw", 0)
            routing_score = data.get("routing_score", 0)

            if not (0.50 <= sr < 0.70) or attempts < 3:
                continue
            if is_pair_contested(src, tgt):
                continue  # CONTESTED-PAIR GATE (Option 1)
            flow_norm_t3 = data.get("flow_norm", 0)
            if attempts > ATTEMPT_THRESHOLD and flow_norm_t3 / max_flow_norm >= HIDDEN_BRIDGE_FLOW_PCT:
                continue

            text = f"[TRANSFER from {src}] Does {src.replace('_', ' ')} transfer to {tgt.replace('_', ' ')}? ({sr:.0%} success, {attempts} transfers)"

            src_h = src.replace('_', ' ').lower()
            tgt_h = tgt.replace('_', ' ').lower()
            if (src_h, tgt_h) in existing_pairs:
                continue
            if queue_domain_counts.get(tgt, 0) >= SATURATION_THRESHOLD:
                continue

            # Prioritize hidden bridges (flow=0) and higher routing_score
            tier3_candidates.append((-routing_score, -attempts, key, sr, attempts, flow, routing_score, text, src, tgt))

        tier3_candidates.sort()
        for _, _, key, sr, attempts, flow, routing_score, text, src, tgt in tier3_candidates[:remaining]:
            if injected >= effective_cap:
                break
            # Reserve slots for Tier 4 (2026-06-20): stop Tier 3 when
            # remaining slots are needed for Tier 4 minimum quota.
            if effective_cap - injected <= MIN_TIER4_SLOTS:
                break
            if queue_domain_counts.get(tgt, 0) >= SATURATION_THRESHOLD:
                continue
            item = {"text": text, "source_domain": src, "target_domain": tgt,
                    "source": "moderate_success", "injected_at": time.time(),
                    "routing_score": routing_score, "success_rate": sr}
            new_items.append(item)
            queue.append(item)
            src_h = src.replace('_', ' ').lower()
            tgt_h = tgt.replace('_', ' ').lower()
            existing_pairs.add((src_h, tgt_h))
            queue_domain_counts[tgt] = queue_domain_counts.get(tgt, 0) + 1
            injected += 1
            phase3_count += 1

    # --- Tier 4: Refutation-boost (SR < 50%, attempts >= 2) ---
    # (formerly Tier 3 — moved after moderate to prioritize hidden bridges)
    # Confirmation cascade fix (2026-06-19): The system currently injects only
    # from high-success-rate domain pairs (4.4% of transfer space). This creates
    # a confirmation cascade where refuted transfers are structurally suppressed.
    # Tier 4 opens up the other 93.9% of transfer space by injecting from
    # low-success-rate pairs. This tests whether the system's topology is
    # determined by routing policy (GPT's hypothesis) rather than organic
    # question evolution.
    #
    # Tagged with source="refutation_boost" for tracking and comparison
    # against control (Tier 1+2 curiosities).
    phase4_count = 0
    if injected < effective_cap:
        remaining = effective_cap - injected
        tier4_candidates = []
        for key, data in cache.items():
            parts = key.split("->")
            if len(parts) != 2:
                continue
            src, tgt = parts
            sr = data.get("success_rate", 0)
            attempts = data.get("attempts", 0)
            flow = data.get("flow_raw", 0)
            routing_score = data.get("routing_score", 0)

            # Tier 4: low success rate, at least 2 attempts
            if sr >= 0.50 or attempts < 2:
                continue
            if is_pair_contested(src, tgt):
                continue  # CONTESTED-PAIR GATE (Option 1)
            flow_norm_t4 = data.get("flow_norm", 0)
            if attempts > ATTEMPT_THRESHOLD and flow_norm_t4 / max_flow_norm >= HIDDEN_BRIDGE_FLOW_PCT:
                continue

            text = f"[TRANSFER from {src}] Does {src.replace('_', ' ')} transfer to {tgt.replace('_', ' ')}? (REFUTATION-BOOST: {sr:.0%} success, {attempts} transfers)"

            src_h = src.replace('_', ' ').lower()
            tgt_h = tgt.replace('_', ' ').lower()
            if (src_h, tgt_h) in existing_pairs:
                continue

            # Queue-state feedback: skip saturated target domains
            if queue_domain_counts.get(tgt, 0) >= SATURATION_THRESHOLD:
                continue

            # Sort by attempts DESC (more refuted attempts = higher priority)
            tier4_candidates.append((-attempts, -routing_score, key, sr, attempts, flow, routing_score, text, src, tgt))

        tier4_candidates.sort()
        for _, _, key, sr, attempts, flow, routing_score, text, src, tgt in tier4_candidates[:remaining]:
            if injected >= effective_cap:
                break
            if queue_domain_counts.get(tgt, 0) >= SATURATION_THRESHOLD:
                continue
            item = {"text": text, "source_domain": src, "target_domain": tgt,
                    "source": "refutation_boost", "injected_at": time.time(),
                    "routing_score": routing_score, "success_rate": sr}
            new_items.append(item)
            queue.append(item)
            src_h = src.replace('_', ' ').lower()
            tgt_h = tgt.replace('_', ' ').lower()
            existing_pairs.add((src_h, tgt_h))
            queue_domain_counts[tgt] = queue_domain_counts.get(tgt, 0) + 1
            injected += 1
            phase4_count += 1

    # Write ONLY to the database.  sync_curiosity_views.py is the single
    # writer for self_state.json and will pick up injected items on its
    # next pass (every 2 min).  Removing the self_state.json write here
    # eliminates the race where inject appends items without updating
    # curiosity_queue_size / metrics.curiosities_active, causing the
    # queue array to drift from the metadata fields.
    # (2026-06-20 fix: corruption observed at 236 items in array vs 123
    # in metadata fields after concurrent sync+inject writes.)

    # Write to the database so sync doesn't overwrite
    if new_items:
        try:
            conn = get_db(DB_PATH)
            now = time.time()
            for item in new_items:
                # Find parent curiosity from text overlap
                parent_id = None
                item_words = item["text"][:60]
                prow = conn.execute(
                    "SELECT id FROM curiosities WHERE text LIKE ? AND source_experiment LIKE 'exp_%' ORDER BY id DESC LIMIT 1",
                    (item_words + "%",)
                ).fetchone()
                if prow:
                    parent_id = prow[0]
                
                # Generation throttle (source-aware: refutation-boost gets own quota)
                _throttled = False
                try:
                    from generation_throttle import should_throttle
                    _item_source = item.get("source", "opportunity_injection")
                    _throttled = should_throttle(conn, source=_item_source)
                except Exception:
                    pass
                
                if _throttled:
                    # Don't break — skip this item and continue.
                    # This ensures refutation_boost items (which have their own
                    # quota) can still be inserted even when the main cap is full.
                    continue
                
                conn.execute(
                    "INSERT INTO curiosities (text, priority, status, source_experiment, created_at, parent_curiosity_id) "
                    "VALUES (?, 1, 'active', ?, ?, ?)",
                    (item["text"], item.get("source", "opportunity_injection"), now, parent_id)
                )
            conn.commit()
            conn.close()
            tier1_count = injected - phase2_count - phase3_count - phase4_count
            parts_label = []
            if tier1_count: parts_label.append(f"80%: {tier1_count}")
            if phase2_count: parts_label.append(f"70%: {phase2_count}")
            if phase3_count: parts_label.append(f"moderate: {phase3_count}")
            if phase4_count: parts_label.append(f"refutation_boost: {phase4_count}")
            tier_label = ", ".join(parts_label) if parts_label else "none"
            print(f"Injected: {injected} ({tier_label}) | Queue: {len(queue)} | DB: {len(new_items)} added")
        except Exception as e:
            print(f"Injected: {injected} | Queue: {len(queue)} | DB error: {e}")
    else:
        print(f"Injected: {injected} | Queue: {len(queue)}")

if __name__ == "__main__":
    main()