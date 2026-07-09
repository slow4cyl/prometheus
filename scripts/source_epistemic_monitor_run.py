#!/usr/bin/env python3
"""
source_epistemic_monitor_run.py — cron entrypoint (no_agent) for
source_epistemic_monitor. Runs in --quiet mode so a CLEAN run produces EMPTY
stdout (the no_agent watchdog contract: empty stdout = silent, nothing delivered
to the user). On an alert it prints the structural-recommendation report, which
the cron then delivers to origin. Always refreshes source_epistemic_status.json
so watchdog_of_watchdogs can read its content regardless of delivery.
"""
import os
import sys
import runpy

sys.argv = [sys.argv[0], "--quiet"]
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
runpy.run_path(
    os.path.expanduser("~/.hermes/scripts/source_epistemic_monitor.py"),
    run_name="__main__",
)
