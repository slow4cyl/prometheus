#!/usr/bin/env python3
"""
pr_watch.py — watch the 14 upstream PRs on NousResearch/hermes-agent (author slow4cyl).

Cron monitor, silent-exit-0 convention:
  * nothing changed            -> no output, exit 0
  * first run                  -> seed the state file silently, exit 0
  * gh missing/unauthed/net    -> fail OPEN: no output, no crash, exit 0
  * something changed          -> one concise line per change on stdout, exit 1
    (PR state became MERGED/CLOSED, a review decision arrived/changed,
     a CI/status check newly became FAILURE)

State file: HERMES_HOME/.pr_watch_state.json
"""
import json
import os
import subprocess
import sys

try:
    from prometheus_paths import HERMES_HOME
except ImportError:  # standalone fallback, same contract as prometheus_paths
    HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))

REPO = "NousResearch/hermes-agent"
AUTHOR = "slow4cyl"
WATCHED = {
    61221, 61222, 61224, 61225, 61226, 61227, 61228,
    61229, 61230, 61231, 61232, 61233, 61234, 61235,
}
STATE_PATH = os.path.join(HERMES_HOME, ".pr_watch_state.json")
GH_TIMEOUT = 60  # seconds


def fetch_prs():
    """Return gh's JSON list of PRs, or None on any failure (fail open)."""
    cmd = [
        "gh", "pr", "list",
        "--repo", REPO,
        "--author", AUTHOR,
        "--state", "all",
        "--limit", "200",  # gh defaults to 30; don't let watched PRs fall off
        "--json", "number,title,state,reviewDecision,statusCheckRollup",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None  # gh missing, unexecutable, or timed out
    if proc.returncode != 0:
        return None  # unauthed, network failure, ...
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def failed_checks(rollup):
    """Names of checks whose outcome is FAILURE.

    statusCheckRollup mixes CheckRun objects (name/conclusion) and
    StatusContext objects (context/state); ERROR on a StatusContext is
    its failure state.
    """
    names = set()
    for check in rollup or []:
        if not isinstance(check, dict):
            continue
        outcome = (check.get("conclusion") or check.get("state") or "").upper()
        if outcome in ("FAILURE", "ERROR"):
            names.add(check.get("name") or check.get("context") or "check")
    return sorted(names)


def snapshot(prs):
    """Reduce gh output to the fields we diff, keyed by PR number (as str)."""
    snap = {}
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("number") not in WATCHED:
            continue
        decision = pr.get("reviewDecision") or ""
        if decision == "REVIEW_REQUIRED":  # default pre-review value, not a decision
            decision = ""
        snap[str(pr["number"])] = {
            "title": pr.get("title") or "",
            "state": (pr.get("state") or "").upper(),
            "reviewDecision": decision,
            "failed_checks": failed_checks(pr.get("statusCheckRollup")),
        }
    return snap


def load_state():
    """Prior snapshot, or None if absent/corrupt (then reseed silently)."""
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_state(snap):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=2, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass  # fail open; worst case we re-diff next run


def diff(old, new):
    """One concise alert line per meaningful change."""
    lines = []
    for key in sorted(new, key=int):
        cur = new[key]
        prev = old.get(key)
        if not isinstance(prev, dict):
            continue  # PR newly appeared in state: seed silently, no alert
        title = cur["title"] or prev.get("title") or ""

        old_state = (prev.get("state") or "").upper()
        if cur["state"] != old_state and cur["state"] in ("MERGED", "CLOSED"):
            lines.append("PR #%s %s: %s" % (key, cur["state"], title))

        old_decision = prev.get("reviewDecision") or ""
        if cur["reviewDecision"] and cur["reviewDecision"] != old_decision:
            lines.append("PR #%s review %s: %s" % (key, cur["reviewDecision"], title))

        new_failures = sorted(
            set(cur["failed_checks"]) - set(prev.get("failed_checks") or [])
        )
        if new_failures:
            lines.append(
                "PR #%s CI FAILURE (%s): %s" % (key, ", ".join(new_failures), title)
            )
    return lines


def main():
    prs = fetch_prs()
    if prs is None:
        return 0  # fail open: gh missing/unauthed/network down

    new = snapshot(prs)
    if not new:
        return 0  # none of the watched PRs came back; don't touch the baseline

    old = load_state()
    if old is None:
        save_state(new)  # first run (or corrupt state): seed silently
        return 0

    lines = diff(old, new)

    # Keep last-known entries for watched PRs absent from this response so a
    # transient gh omission doesn't wipe the baseline; drop unwatched keys.
    watched_keys = {str(n) for n in WATCHED}
    merged = {k: v for k, v in old.items() if k in watched_keys}
    merged.update(new)
    save_state(merged)

    if lines:
        print("\n".join(lines))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
