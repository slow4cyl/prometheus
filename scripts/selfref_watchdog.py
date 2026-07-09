#!/usr/bin/env python3
"""
Self-Referential Bias Watchdog — detects when Prometheus's circular calibration
loop is active and producing inflated statistics.

Based on WR #81130: self-referential source domains reduce transfer success
by 5.1pp (63.1% vs 68.2%, p<0.0001).

This script:
1. Computes transfer success rates for self-referential vs external domains
2. Compares them to the expected 5.1pp gap
3. Reports ONLY when the gap exceeds the threshold (silent when healthy)
4. Writes a timestamped alert log

CRON JOB TEMPLATE: no_agent=true, every 5m, deliver=local
"""

import json
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import os
import sys
import sqlite3
import time

PROMETHEUS_DB = _PP_PROMETHEUS_DB
ALERT_LOG = os.path.expanduser("~/.hermes/selfref_watchdog_alerts.log")
STATE_FILE = os.path.expanduser("~/.hermes/selfref_watchdog_state.json")

SELF_REFERENTIAL_DOMAINS = {
    'meta_analysis', 'calibration', 'cross_domain_transfer',
    'cross_domain_prediction', 'bias', 'cognitive_science',
    'cognitive_bias', 'meta_learning', 'ai_alignment', 'safety',
    'system_health', 'metrics_benchmarking', 'epistemic_architecture',
    'adversarial_ml', 'injection_detection'
}

# Alert when the gap INCREASES by more than this amount relative to the
# last reading. This detects degradation (new bias emerging) without
# firing repeatedly on a known stable condition. The absolute gap is
# still logged for monitoring.
DEGRADATION_THRESHOLD = 4.0  # pp increase since last reading


def compute_transfer_rates():
    """Compute transfer success rates from worker_results findings.
    
    Transfer success is measured as the fraction of experiments where
    the hypothesis was supported (not refuted), parsed from key_finding text
    or inferred from hypothesis_supported.
    """
    con = sqlite3.connect(f"file:{PROMETHEUS_DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    
    # Get experiments with known support/refute AND a domain
    rows = con.execute("""
        SELECT domain, hypothesis_supported, calibrated_confidence
        FROM worker_results
        WHERE hypothesis_supported IS NOT NULL
          AND domain IS NOT NULL AND domain != ''
    """).fetchall()
    
    con.close()
    
    sr_total = 0
    sr_supported = 0
    sr_confidence_sum = 0.0
    sr_conf_n = 0
    
    ext_total = 0
    ext_supported = 0
    ext_confidence_sum = 0.0
    ext_conf_n = 0
    
    for r in rows:
        domain = r['domain']
        supported = r['hypothesis_supported']
        # Do NOT substitute 0.5 for a floored/NULL calibrated_confidence — that
        # silently re-neutralizes the very signal this watchdog exists to surface
        # (a floored cal IS the alarm). Count floored 0.0 as 0.0; skip genuine
        # NULL from the confidence average so it neither fabricates 0.5 nor breaks
        # the sum. (fix 2026-06-25)
        raw_conf = r['calibrated_confidence']
        has_conf = raw_conf is not None
        confidence = raw_conf if has_conf else 0.0

        if domain in SELF_REFERENTIAL_DOMAINS:
            sr_total += 1
            if supported == 1:
                sr_supported += 1
            if has_conf:
                sr_confidence_sum += confidence
                sr_conf_n += 1
        else:
            ext_total += 1
            if supported == 1:
                ext_supported += 1
            if has_conf:
                ext_confidence_sum += confidence
                ext_conf_n += 1
    
    sr_rate = sr_supported / sr_total * 100 if sr_total > 0 else 0
    ext_rate = ext_supported / ext_total * 100 if ext_total > 0 else 0
    sr_avg_conf = sr_confidence_sum / sr_conf_n if sr_conf_n > 0 else 0
    ext_avg_conf = ext_confidence_sum / ext_conf_n if ext_conf_n > 0 else 0
    
    return {
        'sr_domains': len(set(r['domain'] for r in rows if r['domain'] in SELF_REFERENTIAL_DOMAINS)),
        'sr_total': sr_total,
        'sr_supported': sr_supported,
        'sr_rate': sr_rate,
        'sr_avg_conf': sr_avg_conf,
        'ext_total': ext_total,
        'ext_supported': ext_supported,
        'ext_rate': ext_rate,
        'ext_avg_conf': ext_avg_conf,
        'gap': ext_rate - sr_rate,
        'expected_gap': 5.1,  # From WR #81130
    }


def main():
    stats = compute_transfer_rates()
    gap = stats['gap']

    # Load previous gap from state file for degradation detection
    prev_gap = None
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
            prev_gap = state.get('gap')
    except (OSError, json.JSONDecodeError):
        pass

    # Save current gap for next run
    with open(STATE_FILE, "w") as f:
        json.dump({'gap': gap, 'timestamp': time.time()}, f)

    # Degradation = how much the gap increased since last reading
    degradation = (gap - prev_gap) if prev_gap is not None else 0.0

    # Build status line
    status = (
        f"SelfRefWatchdog: SR={stats['sr_rate']:.1f}% "
        f"(n={stats['sr_total']}, conf={stats['sr_avg_conf']:.3f}) | "
        f"EXT={stats['ext_rate']:.1f}% "
        f"(n={stats['ext_total']}, conf={stats['ext_avg_conf']:.3f}) | "
        f"gap={gap:+.1f}pp (prev={prev_gap}, degradation={degradation:+.1f}pp)"
    )

    # Always write heartbeat for freshness monitoring
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    heartbeat = (
        f"[{timestamp}] OK: gap={gap:+.1f}pp "
        f"(degradation={degradation:+.1f}pp, threshold={DEGRADATION_THRESHOLD}pp) "
        f"| SR={stats['sr_rate']:.1f}% EXT={stats['ext_rate']:.1f}%"
    )
    with open(ALERT_LOG, "a") as f:
        f.write(heartbeat + "\n")

    # Alert ONLY when the gap degrades (increases) beyond threshold —
    # a stable known gap is not an alert, only worsening is.
    if degradation >= DEGRADATION_THRESHOLD:
        alert = (
            f"[{timestamp}] ALERT: Self-referential gap degraded by "
            f"{degradation:.1f}pp (>= {DEGRADATION_THRESHOLD}pp threshold)\n"
            f"  Gap was {prev_gap:.1f}pp, now {gap:.1f}pp\n"
            f"  Self-ref domains ({stats['sr_total']} exps): "
            f"{stats['sr_rate']:.1f}% success, avg conf={stats['sr_avg_conf']:.3f}\n"
            f"  External domains ({stats['ext_total']} exps): "
            f"{stats['ext_rate']:.1f}% success, avg conf={stats['ext_avg_conf']:.3f}\n"
            f"  Expected gap from WR #81130: 5.1pp (63.1% vs 68.2%)\n"
            f"  Confidence inflation: SR conf {stats['sr_avg_conf']:.3f} vs "
            f"actual rate {stats['sr_rate']/100:.3f}\n"
        )

        with open(ALERT_LOG, "a") as f:
            f.write(alert + "\n")

        # Print to stdout so cron delivers it
        print(alert)
        sys.exit(1)  # Non-zero exit for monitoring

    if '--heartbeat' in sys.argv:
        print(heartbeat)

    sys.exit(0)


if __name__ == "__main__":
    main()