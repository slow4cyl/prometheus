"""Isolate every test from any real deployment.

HERMES_HOME is pointed at a per-session temp dir BEFORE any script module is
imported (scripts resolve paths at import time), and scripts/ goes on
sys.path. No test may touch a live database — that rule is enforced here, at
the root, not per-test.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="prometheus-tests-")
os.environ["HERMES_HOME"] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
