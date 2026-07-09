#!/usr/bin/env python3
"""topology_refresh.py — hourly producer for the topology surfaces.

topology_full_export.json is LIVE INFRASTRUCTURE (outcome_routing, auto_tune,
calibration_test, topology_health_collector, the :8889 dashboard all read it)
but had NO cron producer — it went 13h+ stale between manual runs, and the 3-D
plate 3.6 days. The full chain measures ~7s, so one job runs all three:
  build_topology_export.py  →  update_topology_html.py  →  update_topology_3d.py
Silent on success (cron-friendly); any step's failure prints and exits nonzero.
"""
import os
import subprocess
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
CHAIN = ("build_topology_export.py", "update_topology_html.py", "update_topology_3d.py")


def main():
    for name in CHAIN:
        r = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, name)],
            capture_output=True, text=True, timeout=240)
        if r.returncode != 0:
            print(f"{name} failed rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
