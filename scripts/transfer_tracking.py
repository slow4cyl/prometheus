#!/usr/bin/env python3
"""
transfer_tracking.py — Lightweight transfer pipeline diagnostic tracking.

Tracks the lifecycle of [TRANSFER]-tagged findings through the pipeline:
  1. Worker result tagged [TRANSFER] → queued (insert row)
  2. Queue item becomes kanban task → task_created (update row)
  3. Task completed with result → completed_same_domain or completed_cross_domain

Usage:
    # In apply_worker_results.py — insert tracking for new [TRANSFER] items
    from transfer_tracking import insert_tracking, update_task_created, update_completed

    # In batch_create_tasks.py — link task to tracking
    update_task_created(source_result_id, target_domain, task_id)

    # In apply_worker_results.py — mark completion
    update_completed(task_id, destination_result_id, destination_domain)
"""

import os
import re
import time
from db_retry import get_db as retry_get_db

DB_PATH = os.path.expanduser("~/.hermes/prometheus.db")


def _get_conn():
    """Get a retry-enabled connection to prometheus.db."""
    return retry_get_db()


def parse_transfer_target(text):
    """Parse target domain from [TRANSFER] text.
    
    Examples:
        "[TRANSFER from calibration] Does X apply to Y?" → "calibration"
        "[TRANSFER] Does X apply to Y?" → None (no explicit target in text)
    
    For items without explicit target, the actual target domain is determined
    by the curiosity scorer's thread classification, not the text.
    
    Returns target domain string or None if not parseable from text.
    """
    if not text:
        return None
    # Match [TRANSFER from DOMAIN] pattern
    m = re.search(r'\[TRANSFER\s+from\s+(\w+)\]', text, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return None


def extract_source_result_id(item):
    """Extract source_result_id from a queue item dict.
    
    Returns int or None.
    """
    if isinstance(item, dict):
        return item.get("source_result_id")
    return None


def _normalize_domain_for_tracking(domain, conn=None):
    """Best-effort normalize a domain string before it is stored in
    transfer_tracking. Defends against vocabulary fragmentation (the source of
    1,001 distinct source_domain values, 2026-06-24 investigation): a raw worker
    /synthesis domain string is folded to its canonical parent via the
    domain_redirects table when a redirect exists.

    Cheap and side-effect-free: a single indexed lookup against domain_redirects.
    Does NOT invent redirects, does NOT call the embedding classifier, NEVER
    raises — on any error or miss it returns the input unchanged, so a tracking
    insert is never blocked by normalization. Legitimate-but-not-yet-canonical
    domains (e.g. reinforcement_learning) pass through untouched, which is
    correct: they are real domains, just not promoted.
    """
    if not domain:
        return domain
    d = domain.strip().lower()
    try:
        _c = conn if conn is not None else _get_conn()
        row = _c.execute(
            "SELECT new_domain FROM domain_redirects WHERE old_domain = ?",
            (d,),
        ).fetchone()
        if conn is None:
            _c.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass  # best-effort; fall through to the raw value
    return d


def insert_tracking(source_result_id, source_domain, target_domain):
    """Insert a transfer tracking row. Dedup via UNIQUE(source_result_id, target_domain).
    
    Returns True if inserted, False if duplicate (already tracked).

    source_domain / target_domain are normalized via domain_redirects before
    storage (2026-06-24) so report queries that group by source_domain don't
    fragment across redirect-equivalent labels.
    """
    if not source_result_id or not source_domain or not target_domain:
        return False
    try:
        conn = _get_conn()
        source_domain = _normalize_domain_for_tracking(source_domain, conn)
        target_domain = _normalize_domain_for_tracking(target_domain, conn)
        conn.execute(
            """INSERT OR IGNORE INTO transfer_tracking
               (source_result_id, source_domain, target_domain, status, created_at)
               VALUES (?, ?, ?, 'queued', ?)""",
            (int(source_result_id), source_domain, target_domain, time.time())
        )
        conn.commit()
        inserted = conn.total_changes > 0
        conn.close()
        return inserted
    except Exception as e:
        print(f"transfer_tracking: insert error: {e}")
        return False


def update_task_created(source_result_id, target_domain, task_id):
    """Update tracking row when a queue item becomes a kanban task.
    
    Matches on source_result_id where target_domain is still the placeholder
    (source_domain from insert) and updates with the actual thread-based target.
    
    Returns True if updated.
    """
    if not source_result_id or not task_id:
        return False
    try:
        conn = _get_conn()
        # Find the tracking row for this source_result_id
        # (may have placeholder target_domain from apply_worker_results)
        conn.execute(
            """UPDATE transfer_tracking
               SET task_id = ?, task_created_at = ?, status = 'task_created',
                   target_domain = ?
               WHERE source_result_id = ? AND task_id IS NULL""",
            (str(task_id), time.time(), target_domain, int(source_result_id))
        )
        conn.commit()
        updated = conn.total_changes > 0
        conn.close()
        return updated
    except Exception as e:
        print(f"transfer_tracking: update_task error: {e}")
        return False


def update_task_created_by_domain(source_domain, target_domain, task_id):
    """Fallback for update_task_created when source_result_id is unavailable.

    Transfer curiosities created by synthesis_merger carry source_result_id=NULL
    on the child curiosity, so task_refiller can't resolve the tracking row by
    source_result_id. This function matches by source_domain (parsed from the
    [TRANSFER from X] text) + target_domain (the routing thread), picking the
    oldest queued row (FIFO) when multiple match.

    Returns True if a row was updated.
    """
    if not source_domain or not task_id:
        return False
    try:
        conn = _get_conn()
        if target_domain and target_domain != "unknown":
            conn.execute(
                """UPDATE transfer_tracking
                   SET task_id = ?, task_created_at = ?, status = 'task_created',
                       target_domain = ?
                   WHERE id = (
                       SELECT id FROM transfer_tracking
                       WHERE source_domain = ? AND target_domain = ? AND task_id IS NULL
                       ORDER BY id ASC LIMIT 1
                   )""",
                (str(task_id), time.time(), target_domain, source_domain, target_domain)
            )
        else:
            conn.execute(
                """UPDATE transfer_tracking
                   SET task_id = ?, task_created_at = ?, status = 'task_created'
                   WHERE id = (
                       SELECT id FROM transfer_tracking
                       WHERE source_domain = ? AND task_id IS NULL
                       ORDER BY id ASC LIMIT 1
                   )""",
                (str(task_id), time.time(), source_domain)
            )
        conn.commit()
        updated = conn.total_changes > 0
        conn.close()
        return updated
    except Exception as e:
        print(f"transfer_tracking: update_task_by_domain error: {e}")
        return False


def update_completed(task_id, destination_result_id, destination_domain, conn=None):
    """Update tracking row when a transfer task completes.

    Sets status to 'completed_cross_domain' or 'completed_same_domain'
    based on whether the destination domain differs from source.

    CONNECTION REUSE (2026-06-25 root-cause fix):
        When called from inside another script's OPEN write transaction
        (apply_worker_results.py holds prometheus.db open across its whole
        batch loop), opening a *second* competing connection here deadlocks:
        the second connection's UPDATE hits "database is locked", db_retry
        exhausts its 2 retries, raises, the except below swallows it, and the
        transfer row silently never flips to completed. This is the
        "functions are correct but call sites silently fail" failure mode.
        Symptom: `transfers completed (24h)` cratered to ~0 while transfers
        kept being created and reaching task_created.

        FIX: callers that already hold an open connection MUST pass it via
        `conn=`. When `conn` is provided we reuse it and DO NOT commit or
        close it — the caller owns the transaction lifecycle. Only when
        `conn is None` (standalone callers) do we open/commit/close our own.

    Returns True if updated.
    """
    if not task_id:
        return False
    own_conn = conn is None
    try:
        if own_conn:
            conn = _get_conn()
        # Look up the tracking row by task_id
        row = conn.execute(
            "SELECT source_domain FROM transfer_tracking WHERE task_id = ?",
            (str(task_id),)
        ).fetchone()
        if not row:
            if own_conn:
                conn.close()
            return False

        source_domain = row[0]
        if destination_domain and source_domain:
            status = 'completed_cross_domain' if destination_domain != source_domain else 'completed_same_domain'
        else:
            status = 'completed_same_domain'

        conn.execute(
            """UPDATE transfer_tracking
               SET destination_result_id = ?, destination_domain = ?,
                   task_completed_at = ?, status = ?
               WHERE task_id = ?""",
            (destination_result_id, destination_domain, time.time(), status, str(task_id))
        )
        if own_conn:
            conn.commit()
            conn.close()
        # When conn was passed in, the caller commits as part of its own
        # batch/transaction — do NOT commit or close here.
        return True
    except Exception as e:
        print(f"transfer_tracking: update_completed error: {e}")
        return False


def get_funnel_stats():
    """Get transfer funnel statistics.
    
    Returns dict with funnel counts.
    """
    try:
        conn = _get_conn()
        row = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN task_id IS NOT NULL THEN 1 ELSE 0 END) as became_tasks,
                SUM(CASE WHEN task_completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed,
                SUM(CASE WHEN status='completed_cross_domain' THEN 1 ELSE 0 END) as cross_domain,
                SUM(CASE WHEN status='completed_same_domain' THEN 1 ELSE 0 END) as same_domain,
                SUM(CASE WHEN task_id IS NULL AND status='queued' THEN 1 ELSE 0 END) as still_queued
            FROM transfer_tracking
        """).fetchone()
        conn.close()
        return {
            "total": row[0] or 0,
            "became_tasks": row[1] or 0,
            "completed": row[2] or 0,
            "cross_domain": row[3] or 0,
            "same_domain": row[4] or 0,
            "still_queued": row[5] or 0,
        }
    except Exception as e:
        print(f"transfer_tracking: funnel stats error: {e}")
        return {}


def get_transfer_backlog():
    """Count active [TRANSFER] curiosities still in the queue (not yet tasks).
    
    Returns integer count of transfer items waiting.
    """
    try:
        conn = _get_conn()
        row = conn.execute("""
            SELECT COUNT(*) FROM curiosities
            WHERE status='active' AND text LIKE '%[TRANSFER%'
        """).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"transfer_tracking: backlog count error: {e}")
        return 0


def compute_adaptive_transfer_cap(backlog_size, base_cap=0.40, max_cap=0.70, backlog_threshold=10000):
    """Scale TRANSFER_CAP up when backlog is deep, capped at max_cap.
    
    When the transfer backlog exceeds backlog_threshold, the cap increases
    linearly from base_cap to max_cap. This prevents the 7.2-day drain
    problem (24 slots/cycle vs 24,898 backlog) while keeping the cap
    bounded to prevent queue flooding.
    
    Args:
        backlog_size: Number of active [TRANSFER] curiosities waiting
        base_cap: Minimum fraction of tasks that can be transfers (default 0.40)
        max_cap: Maximum fraction (default 0.70)
        backlog_threshold: Backlog at which cap starts scaling up (default 10,000)
    
    Returns:
        Float between base_cap and max_cap
    """
    if backlog_size <= 0:
        return base_cap
    excess = max(0, backlog_size - backlog_threshold)
    scale = min(excess / backlog_threshold, 1.0)  # 0.0 to 1.0
    return base_cap + (max_cap - base_cap) * scale


if __name__ == "__main__":
    """CLI: print current funnel stats."""
    stats = get_funnel_stats()
    if not stats:
        print("No transfer tracking data yet.")
    else:
        print("=== Transfer Pipeline Funnel ===")
        print(f"  Total tracked:        {stats['total']}")
        print(f"  Became tasks:         {stats['became_tasks']}")
        print(f"  Completed:            {stats['completed']}")
        print(f"    Cross-domain:       {stats['cross_domain']}")
        print(f"    Same-domain:        {stats['same_domain']}")
        print(f"  Still queued:         {stats['still_queued']}")
        print(f"  Tasks not completed:  {stats['became_tasks'] - stats['completed']}")
        if stats['became_tasks'] > 0:
            print(f"  Task completion rate: {stats['completed']/stats['became_tasks']*100:.1f}%")
        if stats['completed'] > 0:
            print(f"  Cross-domain rate:    {stats['cross_domain']/stats['completed']*100:.1f}%")
