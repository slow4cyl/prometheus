#!/usr/bin/env python3
"""
Generation throttle — caps how many new curiosities can be created per hour.

Purpose: The system generates questions ~6x faster than it answers them (346/hr 
generation vs ~59/hr exploration). This creates a massive backlog of depth-0 
questions that are never explored. The throttle forces the system to go deeper 
into existing lineages rather than constantly spawning new surface-level questions.

Cap: 100 new curiosities per hour (~1.7x exploration rate — still more questions 
than workers can process, but not 6x more).

Benchmark questions are exempt from the throttle.

Source-aware quota (2026-06-19 confirmation cascade fix):
Refutation-boost curiosities (source='refutation_boost') get a guaranteed
minimum quota of 15 slots per hour. This ensures that the refutation-boost
experiment's curiosities actually get executed, rather than being starved
by the rich-get-richer dynamics of the throttle. Without this, worker-generated
and opportunity_injection curiosities consume all 100 slots and refutation-boost
curiosities are never created in the DB.

The DB path uses the canonical prometheus.db (not the kanban symlink).
"""

import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.expanduser("~/.hermes"), "prometheus.db")
GENERATION_CAP_PER_HOUR = 250  # raised from 150 (2026-06-22): system sustained 136-171/hr, old cap was dropping 20-50 valid follow-ups per hour. The 250 cap is the natural-rate + 50% headroom, sized to stay below the 200-item queue cap as a safety ceiling.
REFUTATION_BOOST_QUOTA = 15  # guaranteed slots for refutation-boost curiosities
REFUTATION_FOLLOWUP_QUOTA = 200  # covers all refutations at d11+ (~64/hr * 2 each = 128)

def get_recent_generation_count(conn=None):
    """Count curiosities created in the last hour, excluding benchmark questions."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        close_conn = True
    try:
        one_hour_ago = time.time() - 3600
        count = conn.execute(
            "SELECT COUNT(*) FROM curiosities WHERE created_at > ? AND benchmark_id IS NULL",
            (one_hour_ago,)
        ).fetchone()[0]
        return count
    finally:
        if close_conn:
            conn.close()

def get_refutation_boost_count(conn=None):
    """Count refutation-boost curiosities created in the last hour."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        close_conn = True
    try:
        one_hour_ago = time.time() - 3600
        count = conn.execute(
            "SELECT COUNT(*) FROM curiosities WHERE created_at > ? AND text LIKE '%REFUTATION-BOOST%'",
            (one_hour_ago,)
        ).fetchone()[0]
        return count
    finally:
        if close_conn:
            conn.close()

def get_refutation_followup_count(conn=None):
    """Count refutation-followup curiosities created in the last hour."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        close_conn = True
    try:
        one_hour_ago = time.time() - 3600
        count = conn.execute(
            "SELECT COUNT(*) FROM curiosities WHERE created_at > ? AND provenance LIKE '%refutation_followup%'",
            (one_hour_ago,)
        ).fetchone()[0]
        return count
    finally:
        if close_conn:
            conn.close()

def should_throttle(conn=None, source=None):
    """Returns True if generation should be throttled (cap reached).
    
    Source-aware: refutation-boost and refutation-followup curiosities get
    their own quotas. Other curiosities share the main cap minus reserved
    quotas.
    """
    if source == "refutation_boost":
        rb_count = get_refutation_boost_count(conn)
        return rb_count >= REFUTATION_BOOST_QUOTA
    
    if source == "refutation_followup":
        rf_count = get_refutation_followup_count(conn)
        return rf_count >= REFUTATION_FOLLOWUP_QUOTA
    
    # For general sources, count only generic (non-refutation) curiosities
    # and compare against the main cap. FIX (2026-06-20): The old logic
    # double-counted refutation items — count included ALL curiosities,
    # then effective_cap was reduced by reserved quotas. This throttled
    # generic injection even when well under cap (e.g., 25 generic items
    # vs 100 cap, but 71 total with 46 reserved made effective_cap=54).
    # Now we query generic count directly so the cap comparison is correct.
    one_hour_ago = time.time() - 3600
    generic_count = conn.execute(
        "SELECT COUNT(*) FROM curiosities "
        "WHERE created_at > ? AND benchmark_id IS NULL "
        "AND text NOT LIKE '%REFUTATION-BOOST%' "
        "AND (provenance IS NULL OR provenance NOT LIKE '%refutation_followup%')",
        (one_hour_ago,)
    ).fetchone()[0]
    return generic_count >= GENERATION_CAP_PER_HOUR

def throttle_status(conn=None):
    """Returns a dict with current throttle status for logging."""
    count = get_recent_generation_count(conn)
    rb_count = get_refutation_boost_count(conn)
    rf_count = get_refutation_followup_count(conn)
    return {
        "generated_last_hour": count,
        "cap": GENERATION_CAP_PER_HOUR,
        "throttled": count >= GENERATION_CAP_PER_HOUR,
        "remaining": max(0, GENERATION_CAP_PER_HOUR - count),
        "refutation_boost_created": rb_count,
        "refutation_boost_quota": REFUTATION_BOOST_QUOTA,
        "refutation_boost_remaining": max(0, REFUTATION_BOOST_QUOTA - rb_count),
        "refutation_followup_created": rf_count,
        "refutation_followup_quota": REFUTATION_FOLLOWUP_QUOTA,
        "refutation_followup_remaining": max(0, REFUTATION_FOLLOWUP_QUOTA - rf_count),
    }
