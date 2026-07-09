# Worker VENV Propagation Gap

## Status: FIXED (2026-07-01)

Both root causes fixed. See "Applied Fix" section in the SKILL.md.

## Problem

Workers running background experiments use `/usr/bin/python3` (system python) instead of the hermes venv python. System python has no torch, no sklearn, no GPU sklearn hook. The venv has all of these. The architecture map (lines 602-613) documents a transparent GPU acceleration layer that should reach workers automatically — it doesn't.

## Root Cause

Two different shell invocation paths in the Hermes codebase, with different env propagation:

### Path 1: Foreground terminal tool (works correctly)

`tools/environments/local.py` -> `_run_bash()`:
- Uses `bash -c` (non-login, non-interactive)
- Before every command, sources the **session snapshot** file (`/tmp/hermes-snap-<session>.sh`)
- The snapshot is captured once at `init_session()` (in `tools/environments/base.py` line ~352) by running a login shell (`bash -l -c`), which sources `~/.bashrc` and picks up `VIRTUAL_ENV`
- Every foreground command gets `VIRTUAL_ENV=~/.hermes/hermes-agent/venv` from the snapshot
- `execute_code` tool additionally has `_resolve_child_python()` (in `code_execution_tool.py` line ~1644) which checks `VIRTUAL_ENV` env var and resolves to `$VIRTUAL_ENV/bin/python3`

Snapshot content verified:
```
$ grep VIRTUAL_ENV /tmp/hermes-snap-000119b38d02.sh
declare -x VIRTUAL_ENV="~/.hermes/hermes-agent/venv"
```

### Path 2: Background process spawn (was broken, now fixed)

`tools/process_registry.py` lines ~700, ~744:
- Uses `bash -lic` (login + interactive + command)
- Uses `env = _sanitize_subprocess_env(os.environ, env_vars)` — the GATEWAY's `os.environ`
- Did NOT source the session snapshot
- The gateway process (verified via `/proc/<pid>/environ`) has NO `VIRTUAL_ENV` — it only has the venv's `bin/` on `PATH`
- Login shell (`-l`) sources `~/.bash_profile` / `~/.profile` but these don't activate the venv
- `~/.bashrc` activates the venv, but login shells don't source `~/.bashrc` by default (only interactive non-login shells do)

Result: `python3` resolved to `/usr/bin/python3` (system python), which had no torch, no sklearn, no GPU hook.

## Applied Fix

### Fix 1: `_inject_venv_env()` in process_registry.py

Added a helper function `_inject_venv_env(env)` in `tools/process_registry.py` that:
1. Reads `sys.prefix` (the venv root when running under the hermes venv)
2. Guards: skips if `sys.prefix == sys.base_prefix` (not in a venv) or `venv/bin` doesn't exist
3. Sets `env["VIRTUAL_ENV"] = venv_root`
4. Prepends `venv/bin` to `PATH` (case-insensitive key lookup for Windows compat)

Called at two sites:
- PTY path (line ~700): after `pty_env["PYTHONUNBUFFERED"] = "1"`, before `PtyProcessCls.spawn`
- Popen path (line ~744): after `bg_env["PYTHONUNBUFFERED"] = "1"`, before `subprocess.Popen`

Also added `import sys` to the file's imports.

### Fix 2: GPU sklearn hook re-enabled

Renamed `gpu_sklearn_hook.pth.disabled` -> `gpu_sklearn_hook.pth` in:
`~/.hermes/hermes-agent/venv/lib/python3.14/site-packages/`

Verified: `LogisticRegression.__module__` is now `gpu_sklearn._core`, confirming the hook intercepts sklearn imports.

## Evidence (Pre-Fix)

### Gateway env (PID 886008):
```
$ cat /proc/886008/environ | tr '\0' '\n' | grep -E 'VIRTUAL_ENV|PATH='
PATH=~/.local/bin:~/.hermes/hermes-agent/venv/bin:...
(no VIRTUAL_ENV)
```

### Worker experiment process (ps output):
```
/usr/sbin/bash -lic set +m; cd ~/.hermes/kanban/workspaces/t_5f1df343 && python3 -u exp_cvk_assortativity.py
```
The `bash -lic` pattern comes from `process_registry.py` line 700/744.

### System python3 vs venv python3:
```
$ /usr/bin/python3 -c "import torch"
ModuleNotFoundError: No module named 'torch'

$ ~/.hermes/hermes-agent/venv/bin/python -c "import torch; print(torch.cuda.is_available())"
torch: 2.11.0+cu130
cuda: True
```

### GPU sklearn hook status (pre-fix):
```
$ ls ~/.hermes/hermes-agent/venv/lib/python3.14/site-packages/gpu_sklearn_hook.pth*
gpu_sklearn_hook.pth.disabled   <- DISABLED
```

## Post-Fix Verification

```
$ ~/.hermes/hermes-agent/venv/bin/python -c "
from sklearn.linear_model import LogisticRegression
print(LogisticRegression.__module__)
"
gpu_sklearn._core   # hook is working
```

## The GPU sklearn Hook Architecture (for reference)

From `~/.hermes/scripts/gpu_sklearn/__init__.py`:

- `.pth` file in site-packages runs at Python startup
- Registers a `__import__` hook that intercepts sklearn imports
- After sklearn loads a module, the hook patches GPU classes in
- Workers see the same API — fit(), predict(), score() — all GPU-accelerated

Covered classes (auto-redirected):
- `sklearn.linear_model.LogisticRegression` -> GPU (5-8x on >5K samples)
- `sklearn.preprocessing.StandardScaler` -> GPU (5x on >5K samples)
- `sklearn.decomposition.PCA` -> GPU (9x on >5K samples)
- `sklearn.neighbors.KNeighborsClassifier` -> GPU (2.5x on >5K samples)
- `sklearn.feature_selection.SelectKBest` -> GPU

NOT covered: `sklearn.model_selection`, `sklearn.metrics`, `sklearn.feature_extraction`, `sklearn.pipeline`, `sklearn.utils`, and all other submodules.

The hook is at: `~/.hermes/scripts/gpu_sklearn/_import_hook.py`
The daemon client is at: `~/.hermes/scripts/gpu_sklearn/_client.py`
The daemon itself is at: `~/.hermes/scripts/gpu_sklearn/_gpu_daemon.py`
The daemon runs under: `~/vllm-env/bin/python` (PyTorch 2.11+cu130, Blackwell sm_120)
Socket: `/tmp/gpu_sklearn_daemon.sock`

## Additional Context: Experiment Types

As of 2026-07-01, 25 of 27 running experiments were pure numpy/scipy/networkx (Kuramoto oscillator simulations, network science). Only 2 imported torch, and those didn't call `.cuda()` or set a device. So even with the venv fixed, most experiments would still run on CPU — they have no GPU code path. The GPU acceleration is specifically for sklearn operations, which only some experiments use.

Session 2026-07-01.
