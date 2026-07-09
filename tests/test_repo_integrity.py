"""Repo-level integrity invariants.

Two regression guards born from real gaps:
- cron referenced `calibration_loop.sh`, which never made it into the repo
  (string-invoked scripts are invisible to import-graph staging);
- a hardcoded personal absolute path survived two sanitization passes inside
  a file that entered the repo late.
"""
import json
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(REPO, "scripts")


def _cron_script_names():
    with open(os.path.join(REPO, "cron", "jobs.json")) as fh:
        data = json.load(fh)
    jobs = data["jobs"] if isinstance(data, dict) else data
    for job in jobs:
        script = (job.get("script") or "").strip()
        if script:
            yield job.get("name", "?"), os.path.basename(script.split()[0])


def test_every_cron_script_reference_exists_in_repo():
    missing = [(name, script) for name, script in _cron_script_names()
               if not os.path.exists(os.path.join(SCRIPTS, script))]
    assert not missing, f"cron references scripts absent from scripts/: {missing}"


def test_shell_scripts_pull_their_python_helpers():
    """Scripts executed by shell wrappers (run via bare filenames) must ship."""
    for fn in os.listdir(SCRIPTS):
        if not fn.endswith(".sh"):
            continue
        body = open(os.path.join(SCRIPTS, fn), errors="replace").read()
        for helper in set(re.findall(r"[a-z_0-9]+\.py", body)):
            assert os.path.exists(os.path.join(SCRIPTS, helper)), \
                f"{fn} invokes {helper}, which is not in scripts/"


def test_no_personal_absolute_paths_in_code():
    """Code must not hardcode /home/<user> paths (docs/ may describe them)."""
    offenders = []
    for root in ("scripts", "plugins", "dashboard", "tests"):
        base = os.path.join(REPO, root)
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if not fn.endswith((".py", ".sh", ".yaml", ".json", ".service")):
                    continue
                path = os.path.join(dirpath, fn)
                text = open(path, errors="replace").read()
                for m in re.finditer(r"/home/[a-z0-9_]+/", text):
                    # this test file quotes the pattern in its docstring
                    if os.path.samefile(path, __file__):
                        continue
                    offenders.append(f"{os.path.relpath(path, REPO)}: {m.group(0)}")
    assert not offenders, "hardcoded personal paths:\n" + "\n".join(offenders[:20])
