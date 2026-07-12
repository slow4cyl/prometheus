#!/usr/bin/env python3
"""Watchdog for the Prometheus dashboard (v2, :8889) — matched pair only.

v3 (2026-07-08): the old version was cross-wired — it health-checked :8888
(the v1 dashboard) but its restart action bounced prometheus-dashboard.service,
which serves the v2 dashboard on :8889. So a v1 death could never be revived
AND would bounce the healthy v2 unit every 10 minutes forever, while a hung v2
went undetected. Fixed to matched pairs.

v4 (2026-07-12): v1 RETIRED. The detached v1 process (~/prometheus_dashboard.py,
:8888) had been WEDGED since Jul 08 — alive and holding the port but never
answering — so every watchdog cycle's relaunch died on Address-already-in-use
and nobody noticed for four days, which is the empirical proof nobody uses v1.
Part-12 made v2 the visibility surface; v1's panels also still read the dead
subtopics/gaps tables. The wedged process was killed and both file copies
archived to scripts/_archived-experiments/dashboards/. This watchdog now guards
only the pair that matters: :8889 <-> prometheus-dashboard.service.
Runs silent when healthy (cron-friendly).
"""
import subprocess
import sys
import time

CURL_TIMEOUT = 30       # the dashboard queries the ~1.9GB DB — generous
RETRY_INTERVAL = 10
MAX_RETRIES = 3
V2_SERVICE = "prometheus-dashboard.service"   # owns :8889


def is_alive(port):
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"http://127.0.0.1:{port}/"],
            capture_output=True, text=True, timeout=CURL_TIMEOUT)
        return out.stdout.strip() == "200"
    except Exception:
        return False


def alive_with_retries(port):
    for attempt in range(MAX_RETRIES):
        if is_alive(port):
            return True
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_INTERVAL)
    return False


def wait_for(port, budget=60):
    deadline = time.time() + budget
    while time.time() < deadline:
        if is_alive(port):
            return True
        time.sleep(5)
    return False


def main():
    rc = 0
    # ── v2 on :8889 — the unit-owned dashboard ──
    if not alive_with_retries(8889):
        subprocess.run(["systemctl", "--user", "restart", V2_SERVICE],
                       capture_output=True, timeout=30)
        if wait_for(8889):
            print("v2 dashboard (:8889) restarted via", V2_SERVICE)
        else:
            print("WARNING: v2 dashboard (:8889) failed to come back after unit restart")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
