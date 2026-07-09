#!/usr/bin/env python3
"""Cron entrypoint: refresh the contested_transfer_pairs table.

Runs transfer_convergence.refresh_contested_pairs(). Kept separate from
transfer_convergence.py so the library's default __main__ stays a safe dry-run
(no accidental writes when run by hand).

SILENT BY DESIGN: this is a no_agent cron. Empty stdout => the scheduler sends
nothing to the user (the maintenance pattern). It refreshes the gate table and
stays quiet. A failure raises -> non-zero exit -> the scheduler surfaces an
error alert, so a broken refresh can't fail silently.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from transfer_convergence import refresh_contested_pairs

refresh_contested_pairs()  # writes contested_transfer_pairs; no stdout on success
