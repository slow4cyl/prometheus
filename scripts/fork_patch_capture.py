#!/usr/bin/env python3
"""fork_patch_capture.py — keep the hermes-agent fork patch snapshot fresh.

The ~12 locally-patched base files (architecture-map §Fork modifications) are
reverted by any base update. This re-captures `git diff HEAD` to
~/.hermes/fork-patches/ whenever the diff changes, so the snapshot is never stale
and re-application after an update loses nothing.

Cheap + idempotent + best-effort: a read-only `git diff`, writes only when the
content actually changed, never raises. Cron: `fork-patch-capture` (interval).
"""
import subprocess
import os
import hashlib
import datetime

AGENT = os.path.expanduser("~/.hermes/hermes-agent")
OUT = os.path.expanduser("~/.hermes/fork-patches")
LATEST = os.path.join(OUT, "prometheus-fork-latest.patch")
ARCHIVE = os.path.join(OUT, "archive")
LOG = os.path.join(OUT, "capture.log")


def g(*a):
    return subprocess.run(["git", "-C", AGENT, *a], capture_output=True, text=True).stdout


def _sha(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:12]


def main():
    try:
        os.makedirs(OUT, exist_ok=True)
        # Diff the whole fork delta against the upstream base it sits on — captures the
        # mods whether they are committed (on prometheus-fork) or loose working-tree edits.
        base = (g("merge-base", "HEAD", "origin/main").strip()
                or g("merge-base", "HEAD", "main").strip()
                or "5937b9519")
        patch = g("diff", base)
        if not patch.strip():
            return 0    # fork matches base exactly — nothing to snapshot
        new_sha = _sha(patch)
        old_sha = _sha(open(LATEST).read()) if os.path.exists(LATEST) else None
        if new_sha == old_sha:
            return 0    # unchanged — silent, no churn

        head = g("rev-parse", "HEAD").strip()
        branch = g("rev-parse", "--abbrev-ref", "HEAD").strip()
        stat = g("diff", base, "--stat").rstrip()
        n_files = max(0, len([l for l in stat.splitlines() if "|" in l]))
        date = datetime.date.today().isoformat()

        # always-current snapshot + a dated/sha'd archive copy of each distinct state
        with open(LATEST + ".tmp", "w") as f:
            f.write(patch)
        os.replace(LATEST + ".tmp", LATEST)
        os.makedirs(ARCHIVE, exist_ok=True)
        with open(os.path.join(ARCHIVE, f"prometheus-fork-{date}-{new_sha}.patch"), "w") as f:
            f.write(patch)

        manifest = f"""# Prometheus fork — hermes-agent base patch stack (auto-captured)

Latest snapshot **{date}** (sha `{new_sha}`), refreshed by `fork_patch_capture.py`
(cron `fork-patch-capture`) whenever a base file changes. See architecture-map
§Fork modifications for what each file does.

- applies onto base commit `{head}` (branch `{branch}`, upstream NousResearch)
- current snapshot: `prometheus-fork-latest.patch` ({len(patch)} bytes, {n_files} files)
- history: `archive/prometheus-fork-<date>-<sha>.patch`
- GPU sklearn hook: the ACTIVE hook is `~/vllm-env/lib/python3.14/site-packages/gpu_sklearn_hook.pth` (a venv rebuild / `pip install --force` wipes it). Restore copy kept here as `gpu_sklearn_hook.pth`. See architecture-map §GPU Sklearn Acceleration.
- NOT in this patch (outside the repo, update-proof, no capture needed): the `prometheus-guard` user plugin at `~/.hermes/plugins/prometheus-guard/` + its `prometheus-guard` entry in `config.yaml` `plugins.enabled`. It duplicates the in-file kanban_tools guard via the `pre_tool_call` hook (plugin fires first) — retire fork commit `e6004d504` at the next base update.

## Files (git diff HEAD --stat)
```
{stat}
```

## Re-apply after an upstream update reverts the fork
```bash
cd ~/.hermes/hermes-agent
git apply ~/.hermes/fork-patches/prometheus-fork-latest.patch     # or --3way if upstream moved
git diff HEAD --stat                                              # confirm the fork files are back
```
"""
        with open(os.path.join(OUT, "MANIFEST.md") + ".tmp", "w") as f:
            f.write(manifest)
        os.replace(os.path.join(OUT, "MANIFEST.md") + ".tmp", os.path.join(OUT, "MANIFEST.md"))

        with open(LOG, "a") as f:
            f.write(f"{date} captured sha={new_sha} files={n_files} bytes={len(patch)} base={head[:12]}\n")
        print(f"fork patch changed → captured {new_sha} ({n_files} files, {len(patch)} bytes)")
        return 0
    except Exception as e:  # noqa: BLE001 — best-effort snapshotter, never break the cron tick
        print(f"fork_patch_capture: skipped ({type(e).__name__}: {str(e)[:120]})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
