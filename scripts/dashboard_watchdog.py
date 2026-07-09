#!/usr/bin/env python3
"""Watchdog for BOTH Prometheus dashboards — matched pairs only.

v3 (2026-07-08): the old version was cross-wired — it health-checked :8888
(the v1 dashboard, a DETACHED process: /usr/bin/python3 ~/prometheus_dashboard.py)
but its restart action bounced prometheus-dashboard.service, which serves the
v2 dashboard on :8889. So a v1 death could never be revived AND would bounce
the healthy v2 unit every 10 minutes forever, while a hung v2 went undetected.

Now:
  :8889 (v2, prometheus-dashboard.service) — curl / with retries; on failure
        restart THE UNIT THAT OWNS THE PORT.
  :8888 (v1, detached ~/prometheus_dashboard.py) — curl with retries; on
        failure relaunch the detached process (setsid), report either way.
Runs silent when both are healthy (cron-friendly).
"""
import os
import subprocess
import sys
import time

CURL_TIMEOUT = 30       # both dashboards query the ~1.9GB DB — generous
RETRY_INTERVAL = 10
MAX_RETRIES = 3
V2_SERVICE = "prometheus-dashboard.service"   # owns :8889
V1_SCRIPT = os.path.expanduser("~/prometheus_dashboard.py")   # detached, :8888


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
    # ── v1 on :8888 — the detached legacy dashboard ──
    if not alive_with_retries(8888):
        if os.path.exists(V1_SCRIPT):
            subprocess.Popen(
                ["setsid", "/usr/bin/python3", V1_SCRIPT],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            if wait_for(8888):
                print("v1 dashboard (:8888) relaunched detached")
            else:
                print("WARNING: v1 dashboard (:8888) did not come back after relaunch")
                rc = 1
        else:
            print(f"WARNING: v1 dashboard down and {V1_SCRIPT} missing")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
