#!/usr/bin/env python3
"""mechanism_calibration.py — is the system's overconfidence uniform, or does it
concentrate in particular KINDS of mechanism?

prior_override_report.py already scores worker preregistrations (a signed
predicted_direction committed before the code) against the observed sign on the
SAME key — a framing-independent measure of prior-confirmation vs prior-override.
It breaks that down BY DOMAIN, which feeds the surprise-weighted scheduler. But
domain is an organizational label; the more actionable axis is the mechanism
SHAPE — does the fleet systematically over-trust its priors about thresholds?
scaling laws? cross-domain transfer? A class the system is reliably wrong about
is a class whose confirm-lane priority should be discounted and whose questions
deserve harder scrutiny.

This lane classifies each scored result into a mechanism class by the shape of
its finding text (regex, no LLM — a calibration lens, not a taxonomy authority)
and reports the prior-confirmation rate per class. Low confirmation = the system's
prior is often WRONG for that shape = an over-trusted class.

Scoring is the SAME framing-independent rule prior_override_report uses (and that
the epistemic-conventions memory flags as load-bearing): compare the SIGN of
predicted_direction[k] to the sign of observed_direction[k] on the same key. Do
NOT score the signed direction against the binary hypothesis_supported — a -1
direction still SUPPORTS its hypothesis most of the time, so that mis-scores
negatives.

Read-only. Writes ~/.hermes/mechanism_calibration.json and prints a report.
Timestamps epoch float. No claim/posterior is ever touched — this is measurement.

Usage:
    python3 mechanism_calibration.py
    python3 mechanism_calibration.py --min-n 10
"""
import argparse
import json
import os
import re
import sqlite3
import time

DB = os.path.expanduser("~/.hermes/prometheus.db")
STATUS_PATH = os.path.expanduser("~/.hermes/mechanism_calibration.json")

# Mechanism classes by finding SHAPE. First match wins, so order = priority:
# the more specific / structurally-distinctive shapes are tested before the
# broad "monotonic relationship" catch-net.
_CLASSES = [
    ("TRANSFER",     re.compile(r'transfer|generaliz|across (domains|fields|scales)|carries over|domain[- ]invariant', re.I)),
    ("THRESHOLD",    re.compile(r'threshold|critical (value|point|ratio|threshold)|onset|tipping|cut[- ]?off|breaks? (at|down|when)|_?crit\b', re.I)),
    ("PHASE",        re.compile(r'\bregime|phase (transition|boundary|diagram)|bistab|hysteresis|bifurcat|transition (at|between|from)', re.I)),
    ("SCALING",      re.compile(r'scal(es|ing|e as)|power[- ]?law|exponent|\bO\(|logarithmic|quadratic|\blinear(ly)?\b|proportional to|grows? (as|with|like)|\b2\*\*|\^d\b', re.I)),
    ("CONSERVATION", re.compile(r'conserv|invariant|symmetr|is conserved|remains? constant|sum (is )?constant', re.I)),
    ("OPTIMUM",      re.compile(r'optim|maximi[sz]|minimi[sz]|\bpeaks?\b|saturat|plateau|sweet spot|extremum', re.I)),
    ("EQUIVALENCE",  re.compile(r'equal to|equivalent|\bidentit|is exactly|\bequals\b|reduces to|same as', re.I)),
    ("MONOTONIC",    re.compile(r'increase|decrease|correlat|monotonic|positively|negatively|rises? with|falls? with|the (higher|lower|more|less)', re.I)),
]


def classify_mechanism(text):
    t = text or ""
    for name, rx in _CLASSES:
        if rx.search(t):
            return name
    return "OTHER"


def _sign(v):
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    return 1 if f > 0 else (-1 if f < 0 else 0)


def _dir_map(raw):
    """Parse a direction field ({"<key>": +1|-1|0}) → dict of key→sign, or {}."""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        s = _sign(v)
        if s is not None:
            out[k] = s
    return out


def score_confirm(predicted_raw, observed_raw):
    """framing-independent: prior-confirmation iff the predicted sign matches the
    observed sign on a shared key. Returns True (confirm), False (override), or
    None (unscoreable — no shared key with a defined observed sign)."""
    pred = _dir_map(predicted_raw)
    obs = _dir_map(observed_raw)
    if not pred or not obs:
        return None
    shared = [k for k in pred if k in obs]
    if not shared:
        return None
    # score on the first shared key (single-key is the overwhelming norm)
    k = shared[0]
    return pred[k] == obs[k]


def main():
    ap = argparse.ArgumentParser(description="Prior-confirmation calibration by mechanism class")
    ap.add_argument("--min-n", type=int, default=8,
                    help="minimum scored rows for a class to be reported (default 8)")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute("""
        SELECT predicted_direction, observed_direction, key_finding, finding, domain
        FROM worker_results
        WHERE predicted_direction LIKE '{%}'
          AND observed_direction IS NOT NULL
    """).fetchall()
    conn.close()

    by_class = {}
    total_scored = total_conf = 0
    for r in rows:
        verdict = score_confirm(r["predicted_direction"], r["observed_direction"])
        if verdict is None:
            continue
        cls = classify_mechanism((r["key_finding"] or "") + " " + (r["finding"] or ""))
        b = by_class.setdefault(cls, {"n": 0, "confirmed": 0})
        b["n"] += 1
        b["confirmed"] += 1 if verdict else 0
        total_scored += 1
        total_conf += 1 if verdict else 0

    overall = round(100.0 * total_conf / total_scored, 1) if total_scored else None
    classes = {
        c: {"n": v["n"],
            "confirmation_pct": round(100.0 * v["confirmed"] / v["n"], 1),
            # gap vs the overall rate: negative = MORE overridden than average =
            # a mechanism shape the system's prior is worse at (over-trusted).
            "vs_overall_pp": (round(100.0 * v["confirmed"] / v["n"] - overall, 1)
                              if overall is not None else None)}
        for c, v in by_class.items()
    }
    status = {
        "overall_confirmation_pct": overall,
        "total_scored": total_scored,
        "by_mechanism": dict(sorted(classes.items(), key=lambda kv: kv[1]["confirmation_pct"])),
        "min_n": args.min_n,
        "note": ("confirmation is an UPPER bound (post-hoc direction backfilling only "
                 "inflates it); a LOW class rate is the trustworthy signal — a mechanism "
                 "shape whose priors the fleet should not be credited for."),
        "last_updated": time.time(),
    }
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, STATUS_PATH)

    print(f"prior-confirmation by mechanism class  (overall {overall}% on {total_scored} scored)")
    print(f"wrote {STATUS_PATH}\n")
    print(f"  {'class':14s} {'n':>6s} {'confirm%':>9s} {'vs overall':>11s}")
    for c, v in status["by_mechanism"].items():
        if v["n"] < args.min_n:
            continue
        flag = "  <-- over-trusted" if (v["vs_overall_pp"] or 0) <= -5 else ""
        print(f"  {c:14s} {v['n']:>6d} {v['confirmation_pct']:>8.1f}% {v['vs_overall_pp']:>+10.1f}pp{flag}")
    return 0


if __name__ == "__main__":
    main()
