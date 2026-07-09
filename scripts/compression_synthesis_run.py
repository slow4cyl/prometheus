#!/usr/bin/env python3
"""
compression_synthesis_run.py — cron entrypoint (no-agent) for the compression phase.

Runs compression_synthesis.py --apply with the SCIENCE venv python (which has
numpy/scipy/sklearn + the embedding client), regardless of what python the
gateway spawns this wrapper with. Prints a one-line summary; no-agent cron
delivers stdout verbatim. Non-zero exit surfaces an alert; the watchdog (§0c)
catches total staleness.
"""
import json
import os
import subprocess
import sys

SCRIPT = os.path.expanduser("~/.hermes/scripts/compression_synthesis.py")

# Prefer the science venv (has sklearn/scipy/numpy); fall back to whatever runs us.
_CANDIDATES = [
    os.path.expanduser("~/.hermes/venv/bin/python3"),
    sys.executable,
    "python3",
]


def _pick_python():
    for p in _CANDIDATES:
        if p in ("python3",) or os.path.exists(p):
            # Verify it actually has the heavy deps before committing to it.
            try:
                r = subprocess.run(
                    [p, "-c", "import numpy,scipy,sklearn"],
                    capture_output=True, timeout=30)
                if r.returncode == 0:
                    return p
            except Exception:
                continue
    return None


def main():
    py = _pick_python()
    if not py:
        print("compression-synthesis: FATAL — no python with numpy/scipy/sklearn found",
              file=sys.stderr)
        sys.exit(2)

    try:
        r = subprocess.run([py, SCRIPT, "--apply", "--json"],
                           capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        print("compression-synthesis: TIMEOUT after 900s", file=sys.stderr)
        sys.exit(1)

    if r.returncode != 0:
        sys.stderr.write(r.stderr or "compression-synthesis: failed\n")
        sys.exit(r.returncode)

    try:
        # Robust: find the JSON object even if a stray line leaked to stdout.
        raw = r.stdout.strip()
        start = raw.find("{")
        s = json.loads(raw[start:]) if start >= 0 else json.loads(raw)
        print(f"compression-synthesis: {s['claims_embedded']} claims -> "
              f"{s['clusters_found']} clusters -> {s['bottlenecks_found']} bottlenecks | "
              f"injected {s['unifying_questions_injected']} unifying + "
              f"{s['boundary_questions_injected']} boundary (run {s['run_id']})")
    except Exception:
        print((r.stdout or "compression-synthesis: ran, no summary").strip()[:300])


if __name__ == "__main__":
    main()
