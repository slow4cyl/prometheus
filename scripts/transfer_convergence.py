#!/usr/bin/env python3
"""
transfer_convergence.py — per-transfer-pair convergence gate (Option 1).

PROBLEM
  The transfer injectors (inject_opportunities.py, tiers 1-4) re-select a
  transfer pair for re-testing based only on its MEAN success_rate and its
  attempt COUNT (from routing_outcome_cache.json). A pair sitting at ~50-70%
  mean success looks "moderately interesting, keep probing" forever — but for
  an ILL-POSED question (e.g. "does machine_learning transfer to motor_learning?")
  the per-experiment verdict is a coin flip (0%..100% depending on which
  mechanisms a worker happens to pick and how it scores them). The mean hides
  this. Result: one non-converging question gets re-run hundreds of times
  (ml->motor: 884 experiments, 58% positive, near-perfect coin flip) while
  producing pure noise. This is the confirmation-cascade / false-convergence
  pathology surfacing as queue spam.

  Re-running an ill-posed question cannot converge by construction, so the fix
  is NOT tighter dedup (transfers deliberately skip the embedding dedup layer —
  see pre_check_dedup.py:421 — because distinct transfer pairs legitimately
  share vocabulary). The fix is a per-pair CAP gated on count + non-convergence.

METRIC
  For each (source, target) transfer pair we parse every completed transfer
  experiment's verdict (REFUTED=0, CONFIRMED/SUPPORTED/PARTIAL*=1) and compute:
    n         = number of verdicted experiments for the pair
    pos_rate  = fraction positive (this is ~ the cache success_rate)
    coinflip  = 1 - |pos_rate - 0.5| * 2
                -> 1.0 at a perfect 50/50 split, 0.0 when unanimous
  A pair is CONTESTED (ill-posed, stop re-injecting) when:
    n >= MIN_N  AND  coinflip >= COINFLIP_THRESHOLD

  This catches the noise (ml->motor: n=884 coinflip=0.83) and SPARES genuinely
  convergent high-volume pairs (enzyme_kinetics->chemical_kinetics: n=249
  pos=0.90 coinflip=0.19; physical_dynamics->fluid_dynamics: n=78 pos=0.96
  coinflip=0.08; financial_trading->group_judgment: n=85 pos=0.16 coinflip=0.33
  — a stable REFUTED). Thresholds validated 2026-06-26 against the live corpus:
  flags 45 of 92 high-N pairs, leaves 47 convergent + all low-N pairs untouched.

DESIGN NOTES
  - Contested pairs are persisted to the `contested_transfer_pairs` table so the
    hot path (is_pair_contested, called per injection candidate) is a cheap
    indexed lookup, not a full-corpus recompute. refresh_contested_pairs()
    recomputes the table; run it periodically (cron) and on demand.
  - This is a COMPASS-vs-GATE-respecting change. It does not stop adversarial
    re-testing of pairs that are still resolving; it only stops pairs that have
    been beaten to death without ever converging. Low-N pairs and convergent
    pairs are never gated.
"""
import os
import re
import time

from db_retry import get_db

HERMES = os.path.expanduser("~/.hermes")
DB_PATH = os.path.join(HERMES, "prometheus.db")

# --- Thresholds (validated against live corpus 2026-06-26) ---
MIN_N = 20                 # need enough runs before declaring a pair ill-posed
COINFLIP_THRESHOLD = 0.60  # 1-|pos-0.5|*2 >= 0.60  <=>  pos in [0.20, 0.80]

_TRANSFER_RE = re.compile(r"\[TRANSFER from ([^\]]+)\].*?transfer to ([^?]+)\?", re.IGNORECASE)


def _norm(s):
    return (s or "").strip().lower().replace(" ", "_")


def parse_pair(text):
    """Extract (source, target) from a transfer hypothesis/question string.

    Returns (src, tgt) normalized to lowercase_underscore, or None.
    """
    if not text:
        return None
    m = _TRANSFER_RE.search(text)
    if not m:
        return None
    return (_norm(m.group(1)), _norm(m.group(2)))


def _binary_verdict(result):
    """REFUTED -> 0 ; CONFIRMED/SUPPORTED/PARTIAL* -> 1 ; else None (ignored)."""
    u = (result or "").upper().lstrip()
    if u.startswith("REFUTED"):
        return 0
    if u.startswith(("CONFIRMED", "SUPPORTED", "PARTIALLY", "PARTIAL")):
        return 1
    return None


def compute_pair_stats(conn=None):
    """Compute per-pair convergence stats from the experiments table.

    Returns dict: (src, tgt) -> {"n", "pos_rate", "coinflip", "contested"}.
    Reuses caller's conn if passed (does NOT close it); otherwise opens+closes.
    """
    own = conn is None
    if own:
        conn = get_db(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT hypothesis, result FROM experiments "
            "WHERE hypothesis LIKE '%[TRANSFER from%' AND result IS NOT NULL"
        ).fetchall()
    finally:
        if own:
            conn.close()

    acc = {}
    for hyp, result in rows:
        pair = parse_pair(hyp)
        if not pair:
            continue
        v = _binary_verdict(result)
        if v is None:
            continue
        tot, pos = acc.get(pair, (0, 0))
        acc[pair] = (tot + 1, pos + v)

    stats = {}
    for pair, (n, pos) in acc.items():
        if n < 2:
            continue
        pos_rate = pos / n
        coinflip = 1.0 - abs(pos_rate - 0.5) * 2.0
        contested = (n >= MIN_N and coinflip >= COINFLIP_THRESHOLD)
        stats[pair] = {
            "n": n,
            "pos_rate": round(pos_rate, 4),
            "coinflip": round(coinflip, 4),
            "contested": contested,
        }
    return stats


def _ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contested_transfer_pairs (
            source       TEXT NOT NULL,
            target       TEXT NOT NULL,
            n            INTEGER NOT NULL,
            pos_rate     REAL NOT NULL,
            coinflip     REAL NOT NULL,
            updated_at   REAL NOT NULL,
            PRIMARY KEY (source, target)
        )
        """
    )


def refresh_contested_pairs(conn=None, verbose=False):
    """Recompute pair stats and replace the contested_transfer_pairs table with
    the CONTESTED pairs only. Returns the list of contested (src, tgt, n,
    pos_rate, coinflip) tuples.
    """
    own = conn is None
    if own:
        conn = get_db(DB_PATH)
    try:
        _ensure_table(conn)
        stats = compute_pair_stats(conn=conn)
        now = time.time()
        contested = [
            (s, t, d["n"], d["pos_rate"], d["coinflip"])
            for (s, t), d in stats.items() if d["contested"]
        ]
        # Full replace: a pair can converge over time and leave the set.
        conn.execute("DELETE FROM contested_transfer_pairs")
        conn.executemany(
            "INSERT INTO contested_transfer_pairs "
            "(source, target, n, pos_rate, coinflip, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(s, t, n, pr, cf, now) for (s, t, n, pr, cf) in contested],
        )
        conn.commit()
        if verbose:
            contested.sort(key=lambda x: -x[2])
            print(f"contested_transfer_pairs refreshed: {len(contested)} pairs "
                  f"(of {len(stats)} pairs with n>=2)")
            for s, t, n, pr, cf in contested[:25]:
                print(f"   n={n:4} pos={pr:.2f} coinflip={cf:.2f}  {s}->{t}")
        return contested
    finally:
        if own:
            conn.close()


# --- Hot-path lookup (cached per process) ---
_CONTESTED_CACHE = None
_CACHE_AT = 0.0
_CACHE_TTL = 300  # seconds


def _load_contested(conn=None):
    own = conn is None
    if own:
        conn = get_db(DB_PATH)
    try:
        _ensure_table(conn)
        return {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT source, target FROM contested_transfer_pairs"
            ).fetchall()
        }
    finally:
        if own:
            conn.close()


def is_pair_contested(src, tgt, conn=None, use_cache=True):
    """Cheap lookup: is (src, tgt) a known ill-posed / non-converging pair?

    src/tgt are normalized internally, so callers may pass either the human
    ("machine learning") or canonical ("machine_learning") form.
    """
    global _CONTESTED_CACHE, _CACHE_AT
    pair = (_norm(src), _norm(tgt))
    if use_cache:
        now = time.time()
        if _CONTESTED_CACHE is None or (now - _CACHE_AT) > _CACHE_TTL:
            _CONTESTED_CACHE = _load_contested(conn=conn)
            _CACHE_AT = now
        return pair in _CONTESTED_CACHE
    return pair in _load_contested(conn=conn)


if __name__ == "__main__":
    import sys
    if "--refresh" in sys.argv:
        refresh_contested_pairs(verbose=True)
    elif "--check" in sys.argv:
        i = sys.argv.index("--check")
        s, t = sys.argv[i + 1], sys.argv[i + 2]
        print(f"{s}->{t}: contested={is_pair_contested(s, t)}")
    else:
        # Default: show what WOULD be contested without writing.
        stats = compute_pair_stats()
        contested = sorted(
            [(s, t, d) for (s, t), d in stats.items() if d["contested"]],
            key=lambda x: -x[2]["n"],
        )
        print(f"{len(contested)} contested pairs (of {len(stats)} with n>=2):")
        for s, t, d in contested[:30]:
            print(f"   n={d['n']:4} pos={d['pos_rate']:.2f} "
                  f"coinflip={d['coinflip']:.2f}  {s}->{t}")
