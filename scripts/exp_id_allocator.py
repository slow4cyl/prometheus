#!/usr/bin/env python3
"""
exp_id_allocator.py — single source of truth for minting experiment IDs.

ROOT-CAUSE FIX (2026-06-08): experiment IDs were minted independently in several
scripts (batch_create_tasks.py, cross_domain_*.py, ...), each doing
`max(int(digits)) + 1` over a SELECT. Two failure modes resulted:
  1. A second ID universe of 17-digit TIMESTAMP-style ids (exp_17809196659xxx)
     polluted max(), so `max+1` produced timestamp-adjacent ids.
  2. When a minter's query happened to see none of those (e.g. a freshly
     quarantined/rebuilt kanban table), max() collapsed and it reset to exp_1,
     exp_2 ... colliding with the oldest experiments via INSERT OR REPLACE.

Fix: ONE allocator that anchors to the SEQUENTIAL series only (ignoring the
>= TIMESTAMP_FLOOR outliers), takes the max across BOTH kanban.db tasks and
prometheus.db experiments, and never returns a value below a persisted
high-water mark. Monotonic and stable regardless of table churn.
"""
import os
from prometheus_paths import HERMES_HOME as _PP_HERMES_HOME
import re
import sqlite3

HERMES = _PP_HERMES_HOME
KANBAN_DB = os.path.join(HERMES, "kanban.db")
PROM_DB = os.path.join(HERMES, "prometheus.db")
HIGHWATER = os.path.join(HERMES, ".exp_id_highwater")

# IDs at or above this are treated as legacy timestamp-style outliers and ignored
# for sequential allocation. The real sequential series is far below this.
TIMESTAMP_FLOOR = 1_000_000

_EXP_RE = re.compile(r"exp_(\d+)")


def _seq_max_from(db_path, sql):
    hi = 0
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        c.execute("PRAGMA busy_timeout=4000")
        for (val,) in c.execute(sql):
            m = _EXP_RE.search(val or "")
            if m:
                n = int(m.group(1))
                if n < TIMESTAMP_FLOOR and n > hi:
                    hi = n
        c.close()
    except Exception:
        pass
    return hi


def _read_highwater():
    try:
        with open(HIGHWATER) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _write_highwater(n):
    try:
        with open(HIGHWATER, "w") as f:
            f.write(str(n))
    except Exception:
        pass


def next_exp_id():
    """Return the next sequential experiment NUMBER (int), monotonic & stable.

    Anchors to the max sequential id across kanban tasks + prometheus experiments
    AND a persisted high-water mark, so a transient empty/rebuilt table can never
    reset the counter to a low (colliding) value.
    """
    k = _seq_max_from(KANBAN_DB, "SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'")
    p = _seq_max_from(PROM_DB, "SELECT id FROM experiments WHERE id GLOB 'exp_[0-9]*'")
    hw = _read_highwater()
    nxt = max(k, p, hw) + 1
    _write_highwater(nxt)  # advance the floor so we never go backwards
    return nxt


if __name__ == "__main__":
    print(next_exp_id())
