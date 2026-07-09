#!/usr/bin/env python3
"""
gpu_sklearn_hook_guard — keep the transparent GPU-sklearn import hook ENABLED
in BOTH the vllm-env and the hermes-agent (worker) venv.

WHY THIS EXISTS (recurring failure):
  The hook is a loose `gpu_sklearn_hook.pth` in each venv's site-packages.
  A venv operation (`pip install -e .`, reinstall, rebuild) WIPES the loose
  .pth, and a stale `gpu_sklearn_hook.pth.disabled` twin then leaves the hook
  OFF. The fork-patch restore only ever covered the vllm-env copy, so the
  hermes-agent (worker) venv copy silently reverted to disabled and workers
  fell back to CPU sklearn — over and over:
    2026-06-28  disabled -> reinstalled
    2026-07-01  disabled again (venv op) -> re-enabled
    2026-07-05  disabled again -> re-enabled + THIS GUARD added
  Workers run in the hermes-agent venv, so that copy is the one that matters.

WHAT IT DOES (idempotent, silent when healthy):
  - Ensures `gpu_sklearn_hook.pth` exists with the canonical content in each venv.
  - Deletes any `gpu_sklearn_hook.pth.disabled` twin (the resurfacing off-switch).
  Runs cheaply from cron; prints only when it had to fix something.
"""
import os

CANON = ("import sys; sys.path.insert(0, os.path.expanduser('~/.hermes/scripts')); "
         "import gpu_sklearn._import_hook\n")

# Both venvs that must carry the hook. python3.14 site-packages.
VENVS = [
    os.path.expanduser("~/vllm-env/lib/python3.14/site-packages"),
    os.path.expanduser("~/.hermes/hermes-agent/venv/lib/python3.14/site-packages"),
]


def guard() -> list:
    fixed = []
    for sp in VENVS:
        if not os.path.isdir(sp):
            continue  # venv not present on this profile
        pth = os.path.join(sp, "gpu_sklearn_hook.pth")
        disabled = pth + ".disabled"
        # Kill the stale off-switch twin so it can't resurface the disabled state.
        if os.path.exists(disabled):
            try:
                os.remove(disabled)
                fixed.append(f"removed stale {disabled}")
            except OSError as e:
                fixed.append(f"FAILED to remove {disabled}: {e}")
        # Restore the active hook if wiped or content-corrupted.
        need_write = True
        if os.path.exists(pth):
            try:
                with open(pth) as f:
                    need_write = f.read().strip() != CANON.strip()
            except OSError:
                need_write = True
        if need_write:
            try:
                with open(pth, "w") as f:
                    f.write(CANON)
                fixed.append(f"restored {pth}")
            except OSError as e:
                fixed.append(f"FAILED to restore {pth}: {e}")
    return fixed


if __name__ == "__main__":
    changes = guard()
    if changes:
        print("gpu_sklearn_hook_guard:", "; ".join(changes))
