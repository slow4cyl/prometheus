#!/usr/bin/env python3
"""
Script-drift sentinel for the live Prometheus script surface.

Standing guard for the deployed-code lane: on 2026-07-11 an A1 worker hit a
routine argparse error calling write_worker_result.py, then "fixed" it by
rewriting the shared live script in place — dropping verify_artifacts() and
calibrate_confidence(). Every dependent cron (calibration retrain, artifact
maintenance, scattered harvest) failed on import for 2.5 hours, and 456
worker_results were written with hardcoded calibration, no artifact
verification, and no kanban_task_id backlink. The gated repo copy was intact
the whole time; nothing compared it against what was actually running.

This sentinel closes that gap: every *.py/*.sh under the repo clone's
scripts/ dir that is also deployed to <HERMES_HOME>/scripts/ must be
byte-identical to the repo copy. The repo is the source of truth
(edit + gate in repo -> atomic deploy to live); live-side tunes must be
synced back to the repo, which keeps this sentinel silent.

Checks:
  1. script_drift    — a co-present script's live bytes differ from the repo
                       copy (one alert per file, with sizes + live mtime).
  2. (info only)     — repo scripts with no live counterpart are reported in
                       the JSON report, not alerted: some repo scripts are
                       repo-only tooling and deploy gaps are a human call.

Fail-open contract: if the repo clone is missing or unreadable, exit 0
silently — the sentinel can only assert against a repo it can read, and it
must never crash the cron lane. A readable-but-different live script IS
drift and alerts.

Conventions (match config_drift_sentinel.py and sibling monitors): silent +
exit 0 when healthy; on drift, print one "ALERT ..." line per file to stdout
and exit 1 so the cron surfaces it. Always writes
<HERMES_HOME>/script_drift_report.json (best-effort).

Override the repo location via env SCRIPT_DRIFT_REPO.
"""
import hashlib
import json
import os
import sys
import time

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
LIVE_DIR = os.path.join(HERMES_HOME, "scripts")
REPO_DIR = os.environ.get(
    "SCRIPT_DRIFT_REPO", os.path.expanduser("~/Projects/prometheus/scripts"))
REPORT = os.path.join(HERMES_HOME, "script_drift_report.json")

WATCHED_EXTS = (".py", ".sh")


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main():
    # Fail-open: no readable repo -> nothing to assert against.
    if not os.path.isdir(REPO_DIR) or not os.path.isdir(LIVE_DIR):
        return 0

    try:
        repo_files = sorted(
            f for f in os.listdir(REPO_DIR)
            if f.endswith(WATCHED_EXTS)
            and os.path.isfile(os.path.join(REPO_DIR, f)))
    except OSError:
        return 0

    drifted = []
    repo_only = []
    checked = 0
    for name in repo_files:
        rp = os.path.join(REPO_DIR, name)
        lp = os.path.join(LIVE_DIR, name)
        if not os.path.isfile(lp):
            repo_only.append(name)
            continue
        try:
            checked += 1
            if os.path.getsize(rp) == os.path.getsize(lp) and sha256(rp) == sha256(lp):
                continue
            drifted.append({
                "file": name,
                "repo_bytes": os.path.getsize(rp),
                "live_bytes": os.path.getsize(lp),
                "live_mtime": time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(os.path.getmtime(lp))),
            })
        except OSError as e:
            # A vanished/unreadable live file while its repo twin exists is
            # itself a drift-class event; report it rather than crash.
            drifted.append({"file": name, "error": str(e)})

    report = {
        "ts": time.time(),
        "checked": checked,
        "drifted": drifted,
        "repo_only": repo_only,
    }
    try:
        tmp = REPORT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(report, f, indent=1)
        os.replace(tmp, REPORT)
    except OSError:
        pass

    if not drifted:
        return 0

    for d in drifted:
        if "error" in d:
            print(f"ALERT script_drift: {d['file']} unreadable live ({d['error']})")
        else:
            print(
                f"ALERT script_drift: {d['file']} live differs from repo "
                f"(repo {d['repo_bytes']}B vs live {d['live_bytes']}B, "
                f"live mtime {d['live_mtime']}) — a worker or manual edit "
                f"changed a deployed script outside the repo gate; diff "
                f"against ~/Projects/prometheus/scripts/{d['file']} and either "
                f"restore from repo or sync the tune into the repo")
    return 1


if __name__ == "__main__":
    sys.exit(main())
