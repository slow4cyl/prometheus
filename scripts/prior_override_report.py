#!/usr/bin/env python3
"""prior_override_report.py — measure prior-confirmation vs prior-override.

Preregistration: workers commit — BEFORE writing experiment code — a predicted
direction, reported via write_worker_result --predicted-direction. Two field
conventions appear and BOTH are scored:
  * fleet-native (the overwhelming majority): {"<hypothesis_key>": +n | -n} — a
    signed DIRECTION under a hypothesis key. Scored against the OBSERVED sign on
    the SAME key (worker_results.observed_direction): did the measured direction
    match the predicted one. This is framing-independent and self-consistent —
    +1 and -1 predictions both confirm ~75% of the time — whereas naively
    mapping sign→SUPPORTED/REFUTED and scoring against the binary verdict
    mis-scores negatives (a -1 direction still SUPPORTS the hypothesis 65% of the
    time). A predicted 0, or an observed value missing/non-numeric on that key,
    is unscoreable.
  * explicit protocol: {"prediction":"SUPPORTED"|"REFUTED",...} — a verdict call,
    scored against the canonical binary verdict (worker_results.hypothesis_supported).
This report scores those preregistrations against the matching outcome:

  prior-confirmation  prediction == outcome. The worker already believed the
                      answer; the experiment confirmed the prior.
  prior-override      prediction != outcome. The EXPERIMENT beat the prior —
                      the only rows where the pipeline demonstrably added
                      information beyond the worker's training.

Honesty caveat, stated once and built into the numbers: a worker that backfills
its prediction after seeing results inflates CONFIRMATION, never override. So
the confirmation rate is an UPPER bound and the override list is the trustworthy
side of the ledger. Multi-key or non-numeric predicted_direction values (mixed
continuous readouts) are counted unparseable, not force-signed.

Read-only. Writes ~/.hermes/prior_override_status.json and prints a report.

Usage:
  python3 prior_override_report.py [--days 30] [--json-only]
"""
import argparse
import json
import os
import sqlite3
import time

DB = os.path.expanduser("~/.hermes/prometheus.db")
STATUS_PATH = os.path.expanduser("~/.hermes/prior_override_status.json")

POS = ("SUPPORTED", "CONFIRMED", "SUPPORT", "TRUE", "YES")
NEG = ("REFUTED", "REFUTE", "REJECTED", "FALSE", "NO")


def _prediction_from_string(raw):
    """Map an explicit-protocol prediction string to 1/0/None."""
    s = str(raw or "").strip().upper()
    if any(s.startswith(p) for p in POS):
        return 1
    if any(s.startswith(n) for n in NEG):
        return 0
    return None


def _dirlabel(v):
    """Display glyph for a signed direction / verdict value."""
    return {1: "+", -1: "-", 0: "0"}.get(v, "?")


def _observed_sign(raw, key):
    """Sign in {-1, 0, 1} of observed_direction[key], or None if the field is
    absent / non-numeric on that key. Observed 0 is a real value (no effect)."""
    try:
        od = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(od, dict) or key not in od:
        return None
    v = od[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return 1 if v > 0 else (-1 if v < 0 else 0)


def score_row(predicted_raw, observed_raw, hypothesis_supported):
    """Score one preregistration. Returns (pred, outcome, matched, label) or
    None if unscoreable. Two conventions, two matching outcomes:

      * explicit {"prediction":"SUPPORTED"|"REFUTED"} — a verdict call, scored
        against the canonical binary verdict (hypothesis_supported). pred/outcome
        in {0,1}.
      * fleet-native {"<key>": +n|-n} — a signed DIRECTION, scored against the
        OBSERVED sign on the SAME key. pred in {-1,1}, outcome in {-1,0,1}. This
        is framing-independent (a -1 direction is not "predicted-refuted"; it is
        a negative effect that may well SUPPORT the hypothesis) and is the only
        scoring under which +1 and -1 predictions confirm at the same rate."""
    try:
        pd = json.loads(predicted_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(pd, dict) or not pd:
        return None
    if "prediction" in pd:
        pred = _prediction_from_string(pd.get("prediction"))
        if pred is None or hypothesis_supported is None:
            return None
        outcome = 1 if hypothesis_supported else 0
        why = str(pd.get("why", ""))[:200]
        return pred, outcome, pred == outcome, why
    if len(pd) != 1:                       # multi-key readout — no single direction to sign
        return None
    k, v = next(iter(pd.items()))
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
        return None                        # non-numeric or an explicit abstain
    ov = _observed_sign(observed_raw, k)
    if ov is None:
        return None                        # nothing measured on the predicted key
    pred = 1 if v > 0 else -1
    return pred, ov, pred == ov, str(k)[:200]


def main():
    ap = argparse.ArgumentParser(description="Prior-confirmation vs prior-override report")
    ap.add_argument("--days", type=int, default=None,
                    help="Only score results from the last N days")
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row

    where_t = ""
    params = []
    if args.days:
        where_t = "AND CAST(wr.created_at AS REAL) > ?"
        params.append(time.time() - args.days * 86400)

    rows = conn.execute(f"""
        SELECT wr.experiment_id, wr.predicted_direction, wr.observed_direction,
               wr.hypothesis_supported, wr.domain, wr.key_finding, wr.finding,
               CAST(wr.created_at AS REAL) AS ts
        FROM worker_results wr
        WHERE wr.predicted_direction LIKE '{{%}}'
          {where_t}
        ORDER BY wr.created_at""", params).fetchall()

    scored = []
    unparseable = 0
    for r in rows:
        res = score_row(r["predicted_direction"], r["observed_direction"],
                        r["hypothesis_supported"])
        if res is None:
            unparseable += 1
            continue
        pred, outcome, matched, label = res
        scored.append({
            "experiment_id": r["experiment_id"],
            "domain": r["domain"],
            "prediction": pred,
            "prediction_confidence": None,   # native direction rows carry no confidence
            "outcome": outcome,
            "matched": matched,
            "why": label or "",
            "finding": str(r["key_finding"] or r["finding"] or "")[:200],
            "ts": r["ts"],
        })

    # Coverage denominator: results in the same window with a verdict at all.
    ts_vals = [s["ts"] for s in scored if s["ts"] is not None]
    if ts_vals:
        t0 = min(ts_vals)
        total_window = conn.execute(
            "SELECT COUNT(*) FROM worker_results "
            "WHERE hypothesis_supported IS NOT NULL "
            "AND CAST(created_at AS REAL) >= ?", (t0,)).fetchone()[0]
    else:
        total_window = 0
    conn.close()

    n = len(scored)
    matched = [s for s in scored if s["matched"]]
    overrides = [s for s in scored if not s["matched"]]

    def mean_conf(items):
        vals = [s["prediction_confidence"] for s in items
                if s["prediction_confidence"] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    by_domain = {}
    for s in scored:
        d = by_domain.setdefault(s["domain"] or "unclassified",
                                 {"n": 0, "matched": 0})
        d["n"] += 1
        d["matched"] += 1 if s["matched"] else 0

    status = {
        "timestamp": time.time(),
        "preregistered_scored": n,
        "unparseable": unparseable,
        "results_in_window": total_window,
        "coverage_pct": round(100.0 * n / total_window, 1) if total_window else None,
        "prior_confirmation_rate_pct":
            round(100.0 * len(matched) / n, 1) if n else None,
        "prior_override_count": len(overrides),
        "mean_prediction_confidence_when_confirmed": mean_conf(matched),
        "mean_prediction_confidence_when_overridden": mean_conf(overrides),
        "by_domain": {k: {"n": v["n"],
                          "confirmation_pct": round(100.0 * v["matched"] / v["n"], 1)}
                      for k, v in sorted(by_domain.items(), key=lambda kv: -kv[1]["n"])},
        "overrides": [{k: s[k] for k in
                       ("experiment_id", "domain", "prediction",
                        "prediction_confidence", "outcome", "why", "finding")}
                      for s in overrides[-50:]],
        "note": ("confirmation rate is an UPPER bound (post-hoc backfilling "
                 "inflates it); overrides are the trustworthy signal"),
    }
    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)

    if args.json_only:
        return 0

    print(f"preregistered & scored: {n}  (unparseable: {unparseable})")
    if not n:
        print("No scoreable preregistrations in window — need a single-key "
              "predicted_direction with a matching observed_direction, or an "
              "explicit {\"prediction\":…}.")
        print(f"status written to {STATUS_PATH}")
        return 0
    print(f"coverage in window:     {n}/{total_window} "
          f"({status['coverage_pct']}% of verdict-bearing results)")
    print(f"prior-confirmation:     {status['prior_confirmation_rate_pct']}%  "
          f"(upper bound — see note)")
    print(f"prior-overrides:        {len(overrides)}")
    if by_domain:
        print("\nby domain (n >= 5):")
        for k, v in status["by_domain"].items():
            if v["n"] >= 5:
                print(f"  {k:28s} n={v['n']:<5d} confirmation={v['confirmation_pct']}%")
    if overrides:
        print("\nmost recent overrides (experiment beat the prior's direction):")
        for s in overrides[-10:]:
            print(f"  {s['experiment_id']}: predicted {_dirlabel(s['prediction'])}, "
                  f"observed {_dirlabel(s['outcome'])} [{s['domain']}] ({s['why']})")
            if s["finding"]:
                print(f"      {s['finding'][:150]}")
    print(f"\nstatus written to {STATUS_PATH}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
