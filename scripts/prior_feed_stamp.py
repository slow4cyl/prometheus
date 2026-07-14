#!/usr/bin/env python3
"""prior_feed_stamp.py — durable record of whether a task carried the RAG
'CONFIRMED PRIOR FINDINGS' feed.

Why: the feed (task_refiller.py / batch_create_tasks.py) pipes earlier confirmed
conclusions into later workers, so their agreement is partly non-independent. The
signal lives in the task BODY — but kanban archives completed tasks and nulls the
body, so the independence gate could only recover it for ~0.4% of the shelf. This
stamps a durable flag at CREATION time, keyed by kanban_task_id, into prometheus.db
(which is not archived). The gate then joins experiments.kanban_task_id → here and
reads prior-fed status regardless of kanban retention.

Contract: called on EVERY task at creation, no gate. Best-effort — never raises,
never blocks task creation. A dropped stamp degrades to 'unmeasurable', which the
gate already handles.
"""
import hashlib
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import re
import sqlite3
import time

DB = _PP_PROMETHEUS_DB
FEED_MARK = "CONFIRMED PRIOR FINDINGS"
_FED_LINE = re.compile(r"^- \[[\d.]+\]\s*(.+)$", re.M)

# Lanes whose tasks must NOT carry the feed (suppress_prior_context). The
# epistemic lanes exist to produce INDEPENDENT evidence: a [CANDIDATE-RETEST]
# fed the original conclusion is not an independent retest (measured 2026-07-04:
# 41/41 recent retest cards carried the feed), and a [BOUNDARY] map fed the
# claim's supports inherits their frame. [CLEAN-ROOM] is the independence
# gate's RAG-suppressed replication lane — blind by definition. [WORLD] is
# the external-data grounding lane (world_grounding.py) — its card carries
# the claim under test but must not carry OTHER findings. Checked by both
# task builders before enrichment; the stamp then records prior_fed=0,
# which is what lets these results count as measured-blind supports.
SUPPRESS_PREFIXES = ("[CANDIDATE-RETEST]", "[BOUNDARY]", "[CLEAN-ROOM]", "[WORLD]",
                     "[SPLIT]")


def suppress_prior_context(task_text):
    """True when this task's lane requires a feed-free (blind) body."""
    return (task_text or "").lstrip().upper().startswith(SUPPRESS_PREFIXES)


def _fed_hashes(body):
    """md5s of the fed finding lines — lets a later audit match whether a claim was
    fed its OWN finding (claim-specific contamination), not just the boolean."""
    seg = body.split(FEED_MARK, 1)[1] if FEED_MARK in body else ""
    return [hashlib.md5(f.strip().encode("utf-8", "replace")).hexdigest()[:12]
            for f in _FED_LINE.findall(seg)]


def record(kanban_task_id, body):
    """Stamp one task. prior_fed = the body carried the feed. Best-effort."""
    tid = str(kanban_task_id or "")
    if not tid.startswith("t_"):
        return
    try:
        fed = FEED_MARK in (body or "")
        hs = _fed_hashes(body or "") if fed else []
        conn = sqlite3.connect(DB, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""CREATE TABLE IF NOT EXISTS task_prior_feed (
            kanban_task_id TEXT PRIMARY KEY,
            prior_fed INTEGER NOT NULL,
            n_fed INTEGER DEFAULT 0,
            fed_hashes TEXT,
            created_at REAL NOT NULL)""")
        conn.execute(
            "INSERT OR REPLACE INTO task_prior_feed "
            "(kanban_task_id, prior_fed, n_fed, fed_hashes, created_at) VALUES (?,?,?,?,?)",
            (tid, 1 if fed else 0, len(hs), json.dumps(hs), time.time()))
        conn.commit()
        conn.close()
    except Exception:
        pass    # instrumentation must never break task creation
