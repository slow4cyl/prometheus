#!/usr/bin/env python3
"""
Watchdog of Watchdogs — checks all critical services and restarts anything dead.

Checks:
  1. Dashboard process (prometheus_dashboard_v2.py on port 8889)
  2. Embedding server (gpu-embed systemd on port 9150)
  3. Dashboard watchdog cron (last run freshness)
  4. Task janitor cron (last run freshness)
  5. Hermes gateway process

Run every 2-3 minutes. Silent when healthy. Reports when something dies.
"""
import subprocess
import os
import signal as _signal
import sys
import time
import urllib.request
import json
from datetime import datetime

# Canonical main hermes dir — derived from script location to avoid HOME override issues
_HERMES_MAIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default
# Resolve HERMES_HOME: use env var if set, but fall back to main dir if profile dir lacks infrastructure
_hermes_home_env = os.environ.get("HERMES_HOME", _HERMES_MAIN)
HERMES_HOME = _hermes_home_env if os.path.exists(os.path.join(_hermes_home_env, "prometheus_db.py")) else _HERMES_MAIN


# Resolve HERMES_HOME: use env var if set (worker subprocesses), fallback to default

HERMES = HERMES_HOME
SCRIPTS = os.path.join(HERMES, "scripts")
KANBAN_DB = os.path.join(HERMES, "kanban.db")


def check_process(name_pattern):
    """Check if a process matching the pattern is running."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", name_pattern],
            capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except Exception:
        return False


def check_port(port, timeout=3):
    """Check if a port is responding."""
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout)
        return req.status == 200
    except Exception:
        return False



def check_user_systemd(service):
    """Check if a user-level systemd service is active."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", service],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False
def check_systemd(service):
    """Check if a systemd service is active."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def restart_dashboard():
    """Restart the Prometheus dashboard via systemd."""
    subprocess.run(
        ["systemctl", "--user", "restart", "prometheus-dashboard.service"],
        capture_output=True, timeout=10
    )


def restart_systemd(service):
    """Restart a systemd service."""
    subprocess.run(["sudo", "systemctl", "restart", service], timeout=15)


def main():
    issues = []
    fixes = []

    # 0. CRITICAL CRON JOB FRESHNESS — check all high-frequency jobs
    try:
        cron_jobs_path = os.path.join(HERMES, "cron", "jobs.json")
        if os.path.exists(cron_jobs_path):
            with open(cron_jobs_path) as _f:
                _cj = json.load(_f)
            _retrigger_count = 0
            for _job in _cj.get("jobs", []):
                if not _job.get("enabled"):
                    continue
                _sched = _job.get("schedule", {})
                _mins = _sched.get("minutes", 999)
                _name = _job.get("name", "unknown")
                _jid = _job.get("id", "")
                _last = _job.get("last_run_at")
                if not _last or _mins > 5:
                    continue  # Skip low-frequency or never-run jobs
                # Parse last_run_at ISO timestamp
                try:
                    from datetime import datetime, timezone
                    _lt = datetime.fromisoformat(_last)
                    _age_min = (datetime.now(timezone.utc) - _lt).total_seconds() / 60
                except Exception:
                    continue
                # Alert if stale: 3x the schedule interval
                _threshold = max(_mins * 3, 3)
                if _age_min > _threshold:
                    issues.append(f"CRON STALE: {_name} (last ran {_age_min:.0f}min ago, threshold {_threshold}min)")
                    # Try to re-run the job (max 2 per cycle to stay under timeout)
                    if _retrigger_count < 2:
                        _retrigger_count += 1
                        try:
                            _r = subprocess.run(
                                ["hermes", "cron", "run", _jid],
                                capture_output=True, text=True, timeout=30
                            )
                            if _r.returncode == 0:
                                fixes.append(f"Re-triggered {_name}")
                            else:
                                fixes.append(f"Re-trigger FAILED for {_name}: {_r.stderr[:100]}")
                        except Exception as _e:
                            fixes.append(f"Re-trigger error for {_name}: {_e}")
    except Exception as _e:
        issues.append(f"Cron freshness check failed: {_e}")

    # 0b. CLAIM GOVERNANCE JOBS — critical governance layer
    #     Only live jobs are listed. Dead entries (claim-evaluator, claim-auto-populate,
    #     validator-selector, replication-tracker) removed — they no longer exist.
    _claim_jobs = {
        "adversarial-auditor": {"max_stale_min": 8, "jid": "39726ebafde7"},
        "literature-grounder": {"max_stale_min": 8, "jid": "edac5ccbb8e7"},
        # Closed-loop confidence calibrator (every 5m). If it stalls, new worker
        # results stop getting freshly-calibrated and the model goes stale.
        "Prometheus confidence calibration — closed-loop retrain":
            {"max_stale_min": 15, "jid": "c7993914d83c"},
        # Compression phase (every 30m). flock-guarded; skips cleanly if overlapped.
        # If genuinely stale (no successful run in ~75min) something is wrong.
        "compression-synthesis": {"max_stale_min": 75, "jid": "97975450c3eb"},
        # Result bridge (every 2m). If it stalls, worker results stop reaching
        # worker_results and apply_worker_results can't process them.
        "result-bridge": {"max_stale_min": 8, "jid": "97cdf25c2cee"},
        # Calibration audit (every 30m). If it stalls, we lose external truth monitoring.
        "calibration-audit": {"max_stale_min": 75, "jid": "e0f94e987b3e"},
        # External probe (every 1h). If it stalls, no fresh benchmark questions.
        "external-probe": {"max_stale_min": 120, "jid": "3e0e07461045"},
    }
    try:
        cron_jobs_path = os.path.join(HERMES, "cron", "jobs.json")
        if os.path.exists(cron_jobs_path):
            with open(cron_jobs_path) as _f:
                _cj = json.load(_f)
            for _job in _cj.get("jobs", []):
                _name = _job.get("name", "")
                if _name not in _claim_jobs:
                    continue
                _last = _job.get("last_run_at")
                _max = _claim_jobs[_name]["max_stale_min"]
                _jid = _claim_jobs[_name]["jid"]
                if not _last:
                    issues.append(f"CLAIM GOVERNANCE: {_name} never ran")
                    continue
                try:
                    from datetime import datetime, timezone
                    _lt = datetime.fromisoformat(_last)
                    _age = (datetime.now(timezone.utc) - _lt).total_seconds() / 60
                    if _age > _max:
                        issues.append(f"CLAIM GOVERNANCE STALE: {_name} (last ran {_age:.0f}min ago, max {_max}min)")
                        try:
                            _r = subprocess.run(["hermes", "cron", "run", _jid],
                                capture_output=True, text=True, timeout=30)
                            if _r.returncode == 0:
                                fixes.append(f"Re-triggered {_name}")
                            else:
                                fixes.append(f"Re-trigger FAILED for {_name}")
                        except Exception as _e:
                            fixes.append(f"Re-trigger error for {_name}: {_e}")
                except Exception:
                    continue
    except Exception as _e:
        issues.append(f"Claim governance check failed: {_e}")

    # 0c. INFREQUENT CRON JOBS — jobs with schedule >5m that §0 skips
    #     These are still critical (backups, audits, monitors). §0 only covers
    #     ≤5m jobs. This section covers the rest with longer thresholds.
    _infrequent_thresholds = {
        10: 30,   # rag-quality-monitor
        15: 45,   # prometheus-db-backup
        30: 90,   # doc-sync, write-leak-detector, self-repair-scanner, queue-*, kanban-db-backup, compression-synthesis
        60: 180,  # confirmation-rate-monitor
        120: 360, # inspector
    }
    try:
        if os.path.exists(cron_jobs_path):
            with open(cron_jobs_path) as _f:
                _cj = json.load(_f)
            for _job in _cj.get("jobs", []):
                if not _job.get("enabled"):
                    continue
                _sched = _job.get("schedule", {})
                _mins = _sched.get("minutes", 999)
                _name = _job.get("name", "unknown")
                _jid = _job.get("id", "")
                _last = _job.get("last_run_at")
                if not _last or _mins <= 5:
                    continue  # §0 handles these
                if _mins not in _infrequent_thresholds:
                    continue
                try:
                    from datetime import datetime, timezone
                    _lt = datetime.fromisoformat(_last)
                    _age_min = (datetime.now(timezone.utc) - _lt).total_seconds() / 60
                except Exception:
                    continue
                _threshold = _infrequent_thresholds[_mins]
                if _age_min > _threshold:
                    issues.append(f"CRON STALE (infrequent): {_name} (last ran {_age_min:.0f}min ago, threshold {_threshold}min, schedule {_mins}m)")
    except Exception as _e:
        issues.append(f"Infrequent cron check failed: {_e}")

    # 0d. CROSS-DOMAIN INJECTOR FRESHNESS — the §0 generic check only fires for
    #     schedules carrying a {minutes: N} field; the staggered cron-expression
    #     jobs ({kind:cron, expr:...}) have no `minutes`, so §0 skips them. The
    #     cross-domain novelty injector (every 5m) feeds exp_NN:[TRANSFER] tasks;
    #     if it stalls, cross-domain transfer generation silently stops. Watch it
    #     explicitly and re-trigger on stall.
    _cdi = {"name": "cross-domain-inject", "jid": "c2ec107d8f4f", "max_stale_min": 15}
    try:
        if os.path.exists(cron_jobs_path):
            with open(cron_jobs_path) as _f:
                _cj = json.load(_f)
            for _job in _cj.get("jobs", []):
                if _job.get("name") != _cdi["name"]:
                    continue
                _last = _job.get("last_run_at")
                if not _last:
                    issues.append(f"CRON STALE: {_cdi['name']} never ran")
                    break
                try:
                    from datetime import datetime, timezone
                    _lt = datetime.fromisoformat(_last)
                    _age = (datetime.now(timezone.utc) - _lt).total_seconds() / 60
                    if _age > _cdi["max_stale_min"]:
                        issues.append(f"CRON STALE: {_cdi['name']} (last ran {_age:.0f}min ago, max {_cdi['max_stale_min']}min)")
                        try:
                            _r = subprocess.run(["hermes", "cron", "run", _cdi["jid"]],
                                capture_output=True, text=True, timeout=30)
                            fixes.append(f"Re-triggered {_cdi['name']}" if _r.returncode == 0
                                         else f"Re-trigger FAILED for {_cdi['name']}")
                        except Exception as _e:
                            fixes.append(f"Re-trigger error for {_cdi['name']}: {_e}")
                except Exception:
                    pass
                break
    except Exception as _e:
        issues.append(f"Cross-domain injector freshness check failed: {_e}")

    # 1. Dashboard process
    if not check_process("prometheus_dashboard_v2.py"):
        issues.append("Dashboard process dead")
        try:
            restart_dashboard()
            fixes.append("Dashboard restarted")
        except Exception as e:
            fixes.append(f"Dashboard restart FAILED: {e}")

    # 2. Dashboard port
    if not check_port(8889):
        issues.append("Dashboard port 8889 not responding")
        try:
            restart_dashboard()
            fixes.append("Dashboard restarted (port fix)")
        except Exception as e:
            fixes.append(f"Dashboard restart FAILED: {e}")

    # 3. Embedding server
    if not check_systemd("gpu-embed"):
        issues.append("Embedding server (gpu-embed) down")
        try:
            restart_systemd("gpu-embed")
            fixes.append("gpu-embed restarted")
        except Exception as e:
            fixes.append(f"gpu-embed restart FAILED: {e}")

    # 4. Embedding server port
    if not check_port(9150):
        issues.append("Embedding server port 9150 not responding")
        try:
            restart_systemd("gpu-embed")
            fixes.append("gpu-embed restarted (port fix)")
        except Exception as e:
            fixes.append(f"gpu-embed restart FAILED: {e}")

    # 5. Hermes gateway
    if not check_user_systemd("hermes-gateway.service"):
        issues.append("Hermes gateway down")
        fixes.append("Gateway needs manual restart (do not auto-restart)")

    # 6. Inspector cron freshness (should run every 2h, alert if >3h stale)
    try:
        inspector_snapshot = os.path.join(HERMES, "inspector/snapshot.json")
        if os.path.exists(inspector_snapshot):
            age_hrs = (time.time() - os.path.getmtime(inspector_snapshot)) / 3600
            if age_hrs > 3:
                issues.append(f"Inspector snapshot stale ({age_hrs:.1f}h since last run, expected <2h)")
        else:
            issues.append("Inspector snapshot.json missing — Inspector may have never run")
    except Exception:
        pass

    # 7. Dashboard watchdog cron freshness
    try:
        # Check last cron output timestamp
        output_dir = os.path.join(HERMES, "cron/output/05b7a0d2dc32")
        if os.path.isdir(output_dir):
            files = sorted(os.listdir(output_dir), reverse=True)
            if files:
                latest = os.path.getmtime(os.path.join(output_dir, files[0]))
                age_min = (time.time() - latest) / 60
                if age_min > 10:  # Should run every 2 min, alert if >10 min stale
                    issues.append(f"Dashboard watchdog stale ({age_min:.0f}min since last run)")
    except Exception:
        pass

    # 8. RAG index freshness — alert if drift > 20 experiments or last index > 10min
    try:
        import sqlite3 as _s3
        prom_db = os.path.join(HERMES, "prometheus.db")
        rag_db = os.path.join(HERMES, "rag", "rag.db")
        if os.path.exists(prom_db) and os.path.exists(rag_db):
            _pc = _s3.connect(f"file:{prom_db}?mode=ro", uri=True, timeout=5)
            _pc.execute("PRAGMA busy_timeout=3000")
            _total = _pc.execute("SELECT COUNT(*) FROM experiments WHERE status='completed'").fetchone()[0]
            _pc.close()
            _rc = _s3.connect(f"file:{rag_db}?mode=ro", uri=True, timeout=5)
            _rc.execute("PRAGMA busy_timeout=3000")
            _indexed = _rc.execute("SELECT COUNT(*) FROM experiments WHERE embedding_file IS NOT NULL").fetchone()[0]
            _rc.close()
            _drift = _total - _indexed
            if _drift > 20:
                issues.append(f"RAG index stale: {_drift} experiments missing embeddings ({_indexed}/{_total} indexed)")
        # Check rag_index_guard last-run timestamp
        guard_stamp = os.path.join(HERMES, ".rag_index_guard_last")
        if os.path.exists(guard_stamp):
            age_min = (time.time() - os.path.getmtime(guard_stamp)) / 60
            if age_min > 10:  # Should run every 2 min, alert if >10 min stale
                issues.append(f"rag_index_guard stale ({age_min:.0f}min since last run)")
    except Exception:
        pass

    # 9. Kanban DB backup freshness — should run every 30m, alert if >60min stale
    try:
        kanban_backup_dir = os.path.join(HERMES, "backups", "kanban-db")
        if os.path.isdir(kanban_backup_dir):
            backup_files = [f for f in os.listdir(kanban_backup_dir) if f.startswith("kanban-") and f.endswith(".db")]
            if backup_files:
                latest_backup = max(backup_files)
                latest_mtime = os.path.getmtime(os.path.join(kanban_backup_dir, latest_backup))
                age_min = (time.time() - latest_mtime) / 60
                if age_min > 60:
                    issues.append(f"Kanban DB backup stale ({age_min:.0f}min since last backup, expected <30min)")
            else:
                issues.append("Kanban DB backup directory has no backups — backup may have never run")
        else:
            issues.append("Kanban DB backup directory missing")
        # Also check kanban.db integrity
        kanban_db = os.path.join(HERMES, "kanban.db")
        if os.path.exists(kanban_db):
            try:
                import sqlite3 as _ks3
                _kdb = _ks3.connect(f"file:{kanban_db}?mode=ro", uri=True, timeout=5)
                _kdb.execute("PRAGMA busy_timeout=3000")
                _kr = _kdb.execute("PRAGMA integrity_check").fetchone()
                _kdb.close()
                if _kr[0] != "ok":
                    issues.append(f"Kanban DB integrity FAILED: {_kr[0]}")
            except Exception as _ke:
                issues.append(f"Kanban DB integrity check error: {_ke}")
    except Exception:
        pass

    # 10. TASK-REFILLER FRESHNESS — check actual output artifact, not cron state
    #     The cron-based check (section 0) can show "Re-triggered" even when
    #     hermes cron run silently fails. This checks the real output file.
    try:
        refiller_summary = os.path.join(HERMES, "refiller_summary.json")
        if os.path.exists(refiller_summary):
            age_min = (time.time() - os.path.getmtime(refiller_summary)) / 60
            if age_min > 5:  # runs every 2 min, alert if >5 min stale
                issues.append(f"TASK-REFILLER STALE: refiller_summary.json {age_min:.0f}min old (expected <2min)")
                # NOTE: Do NOT run task_refiller.py directly as a fallback.
                # The direct execution grabs the file lock, which races with
                # the cron scheduler's own tick — causing "Another instance
                # running, skipping" on both sides.  The hermes cron run
                # trigger (section 0) is sufficient: it sets next_run_at to
                # a past time and the scheduler picks it up on the next tick.
                # If the refiller is stuck, the stuck-script kill switch
                # (section 11) will kill it, allowing the scheduler to retry.
        else:
            issues.append("TASK-REFILLER: refiller_summary.json missing — refiller may have never run")
    except Exception as _e:
        issues.append(f"Refiller freshness check error: {_e}")

    # 11. STUCK-SCRIPT KILL SWITCH — kill no_agent scripts running way past expected runtime
    #     synthesis_merger.py should finish in <2 min, task_refiller.py in <30s,
    #     sync_curiosity_views.py in <1 min, etc. If any of these are still running
    #     after MAX_RUNTIME_MINUTES, they're stuck — kill and let cron restart them.
    _STUCK_SCRIPTS = {
        "synthesis_merger.py":  {"max_min": 5,   "desc": "synthesis merger (expected <2min)"},
        "task_refiller.py":     {"max_min": 5,   "desc": "task refiller (expected <60s, up to ~4min with novelty injection)"},
        "apply_worker_results.py": {"max_min": 10, "desc": "apply worker results (expected <3min)"},
        "sync_curiosity_views.py": {"max_min": 5,  "desc": "curiosity view sync (expected <1min)"},
        "batch_create_tasks.py": {"max_min": 10,  "desc": "batch task creation (expected <3min)"},
        "queue_composition_monitor.py": {"max_min": 5, "desc": "queue composition monitor (expected <30s)"},
        "result_bridge.py": {"max_min": 3, "desc": "result bridge (expected <30s)"},
        "calibration_audit.py": {"max_min": 5, "desc": "calibration audit (expected <30s)"},
        "inject_probe.py": {"max_min": 3, "desc": "inject probe questions (expected <10s)"},
        "calibration_trainer.py": {"max_min": 5, "desc": "calibration trainer (expected <30s)"},
        "backfill_calibration_mv.py": {"max_min": 5, "desc": "calibration backfill (expected <5s)"},
        "compression_synthesis.py": {"max_min": 12, "desc": "compression phase (expected ~80s, flock-guarded)"},
        "topology_health_collector.py": {"max_min": 8, "desc": "topology health collector (expected <2min)"},
        "cross_domain_inject.py": {"max_min": 5, "desc": "cross-domain novelty injector (expected <2min, flock-guarded)"},
    }
    try:
        _ps_out = subprocess.run(
            ["ps", "-eo", "pid,etimes,args", "--no-headers"],
            capture_output=True, text=True, timeout=10
        )
        for _line in _ps_out.stdout.strip().split("\n"):
            if not _line.strip():
                continue
            _parts = _line.strip().split(maxsplit=2)
            if len(_parts) < 3:
                continue
            _pid, _etime_str, _args = _parts
            for _script, _cfg in _STUCK_SCRIPTS.items():
                if _script not in _args:
                    continue
                try:
                    _elapsed_min = int(_etime_str) / 60.0
                except ValueError:
                    continue
                if _elapsed_min > _cfg["max_min"]:
                    issues.append(
                        f"STUCK SCRIPT: {_script} (PID {_pid}) running "
                        f"for {_elapsed_min:.0f}min (max {_cfg['max_min']}min) — killing"
                    )
                    try:
                        os.kill(int(_pid), _signal.SIGKILL)
                        fixes.append(f"Killed stuck {_script} (PID {_pid})")
                    except Exception as _ke:
                        fixes.append(f"Failed to kill {_script} (PID {_pid}): {_ke}")
                    break  # matched — don't check other scripts against same process
    except Exception as _e:
        issues.append(f"Stuck-script check error: {_e}")

    # 12. QUEUE COMPOSITION FRESHNESS — check queue_composition_monitor output
    try:
        qc_status = os.path.join(HERMES, "queue_composition_status.json")
        if os.path.exists(qc_status):
            age_min = (time.time() - os.path.getmtime(qc_status)) / 60
            if age_min > 20:  # runs every 10 min, alert if >20 min stale
                issues.append(f"QUEUE-MONITOR STALE: status file {age_min:.0f}min old (expected <10min)")
                # NOTE: Do NOT run queue_composition_monitor.py directly.
                # Same lock-race issue as task_refiller — the cron scheduler
                # handles re-triggering via hermes cron run.
            else:
                # Read the status and check for a running alert
                try:
                    with open(qc_status) as _f:
                        _qc = json.load(_f)
                    if _qc.get("alert") and _qc.get("escalated"):
                        issues.append(
                            f"QUEUE COMPOSITION: {_qc.get('pct','?')}% synthesis "
                            f"({_qc.get('synthesis_tasks','?')}/{_qc.get('total_tasks','?')}) "
                            f"— escalated to kanban"
                        )
                except Exception:
                    pass
        else:
            issues.append("QUEUE-MONITOR: queue_composition_status.json missing — monitor may have never run")
    except Exception as _e:
        issues.append(f"Queue composition freshness check error: {_e}")

    # 12b. SOURCE EPISTEMIC MONITOR — read CONTENT, not just liveness.
    #     source_epistemic_monitor.py flags generators whose confident output the
    #     calibration model disbelieves at volume. Following the queue_composition
    #     precedent: surface the alert when alert+escalated are set. This is the
    #     check the meta-watchdog historically refused to do — read what a monitor
    #     FOUND, not merely whether it ran.
    try:
        se_status = os.path.join(HERMES, "source_epistemic_status.json")
        if os.path.exists(se_status):
            age_min = (time.time() - os.path.getmtime(se_status)) / 60
            if age_min > 75:  # runs every 30m, alert if >75min stale
                issues.append(f"SOURCE-EPISTEMIC-MONITOR STALE: status file {age_min:.0f}min old (expected <30min)")
            else:
                try:
                    with open(se_status) as _f:
                        _se = json.load(_f)
                    if _se.get("alert") and _se.get("escalated"):
                        srcs = ", ".join(_se.get("flagged_sources", [])) or "?"
                        if _se.get("flagged_count"):
                            issues.append(
                                f"EPISTEMIC: {_se.get('flagged_count','?')} generator(s) "
                                f"producing calibrator-disbelieved output ({srcs}) "
                                f"— structural review (see source_epistemic_status.json)"
                            )
                    # Signal 1: replication-pathology generators (claims fail
                    # independent re-test above baseline) ride the same alert gate.
                    _rp = _se.get("replication_pathology") or []
                    if _rp:
                        _rp_srcs = ", ".join(
                            f"{x['source']} ({int(x['disagree_rate']*100)}% disagree, n={x['n']})"
                            for x in _rp)
                        issues.append(
                            f"EPISTEMIC (replication): {len(_rp)} generator(s) whose claims "
                            f"FAIL INDEPENDENT RE-TEST above baseline ({_rp_srcs}) "
                            f"— structural review (see source_epistemic_status.json)"
                        )
                    _ca = (_se.get("calibration_audit") or {})
                    _brier = _ca.get("brier")
                    _acc = _ca.get("accuracy")
                    if isinstance(_brier, (int, float)) and _brier > 0.30:
                        if isinstance(_acc, (int, float)) and _acc >= 0.80:
                            issues.append(
                                f"CALIBRATION: model is UNDERCONFIDENT — acc {_acc:.2f} but "
                                f"Brier {_brier:.3f} on n={_ca.get('n_with_ground_truth','?')} "
                                f"ground-truth rows (verdicts mostly right, confidence scaled too low). "
                                f"NOTE: calibration_audit_result.json's ~0.45 accuracy is a separate "
                                f"NULL-known_answer artifact, not this number."
                            )
                        else:
                            issues.append(
                                f"CALIBRATION: Brier {_brier:.3f} / acc {_acc} on "
                                f"n={_ca.get('n_with_ground_truth','?')} ground-truth rows "
                                f"(calibrated confidence not tracking correctness)"
                            )
                except Exception:
                    pass
        else:
            issues.append("SOURCE-EPISTEMIC-MONITOR: source_epistemic_status.json missing — monitor may have never run")
    except Exception as _e:
        issues.append(f"Source epistemic monitor check error: {_e}")

    # 12b. BENCH3 CROSS-DOMAIN GAP — alert if gap exceeds 5pp threshold
    try:
        xd_status = os.path.join(HERMES, "bench3_cross_domain_status.json")
        if os.path.exists(xd_status):
            age_min = (time.time() - os.path.getmtime(xd_status)) / 60
            if age_min > 480:  # runs every 6h, alert if >8h stale
                issues.append(f"BENCH3-CROSS-DOMAIN STALE: status file {age_min:.0f}min old (expected <6h)")
            else:
                with open(xd_status) as _f:
                    _xd = json.load(_f)
                if _xd.get("exceeded"):
                    _gap = _xd.get("gap_pp", 0)
                    _in_acc = (_xd.get("in_domain") or {}).get("accuracy", "?")
                    _cr_acc = (_xd.get("cross_domain") or {}).get("accuracy", "?")
                    issues.append(
                        f"BENCH3 CROSS-DOMAIN GAP: {_gap}pp exceeds 5pp threshold "
                        f"(in_domain={_in_acc}%, cross_domain={_cr_acc}%). "
                        f"See bench3_cross_domain_status.json"
                    )
        else:
            issues.append("BENCH3-CROSS-DOMAIN: bench3_cross_domain_status.json missing — monitor may have never run")
    except Exception as _e:
        issues.append(f"Bench3 cross-domain check error: {_e}")

    # 13. PERSISTENT-ISSUE ESCALATION — create kanban tasks for the Director
    #     when the same problem survives multiple watchdog cycles (self-heal
    #     tried but failed). Uses watchdog_state.json for persistence.
    _ESCALATION_THRESHOLD = 3  # kanban task after N consecutive detections
    _PERSISTENT_KEYS = {
        "TASK-REFILLER STALE": "refiller_stale",
        "STUCK SCRIPT": "stuck_script",
        "CRON STALE.*task-refiller": "refiller_cron_stale",
        "QUEUE COMPOSITION": "queue_dominance",
    }
    try:
        # Load state
        _ws_path = os.path.join(HERMES, "watchdog_state.json")
        _ws = {}
        if os.path.exists(_ws_path):
            try:
                with open(_ws_path) as _f:
                    _ws = json.load(_f)
            except Exception:
                _ws = {}
        if "watchdog" not in _ws:
            _ws["watchdog"] = {}

        # Build set of issue signatures detected this cycle
        _detected = set()
        for _issue_text in issues:
            _norm = _issue_text.split(":")[0].strip() if ":" in _issue_text else _issue_text
            # Map to a stable key
            for _pattern, _key in _PERSISTENT_KEYS.items():
                import re as _re
                if _re.search(_pattern, _issue_text):
                    _detected.add(_key)
                    break

        # Update counters: increment detected, reset undetected
        _wd = _ws["watchdog"]
        for _key in set(list(_PERSISTENT_KEYS.values()) + list(_wd.keys())):
            if _key in _detected:
                _wd[_key] = _wd.get(_key, 0) + 1
            else:
                _wd[_key] = 0  # reset — issue cleared

        # Escalate persistent issues
        for _key, _count in list(_wd.items()):
            if _count >= _ESCALATION_THRESHOLD and _count < _ESCALATION_THRESHOLD + 1:
                # First cycle hitting threshold — create kanban task
                _desc = _key.replace("_", " ")
                _title = f"[SELF-HEAL] Persistent issue: {_desc} ({_count} consecutive cycles)"
                _body = (
                    f"The watchdog-of-watchdogs has detected the same issue "
                    f"({_desc}) for {_count} consecutive cycles (~{_count*2} minutes).\n\n"
                    f"Auto-heal attempts were made but the issue keeps recurring.\n"
                    f"Check the watchdog journal for details:\n"
                    f"  journalctl --user -u watchdog-of-watchdogs --since '10 min ago'"
                )
                _tid = "t_" + __import__("uuid").uuid4().hex[:8]
                _now = int(time.time())
                try:
                    _conn = __import__("sqlite3").connect(KANBAN_DB)
                    _conn.execute("PRAGMA busy_timeout=5000")
                    # Dedup: check for existing pending self-heal task with same prefix
                    _existing = _conn.execute(
                        "SELECT COUNT(*) FROM tasks WHERE title LIKE ? "
                        "AND status IN ('ready','running')",
                        (_title[:40] + "%",)
                    ).fetchone()[0]
                    if _existing == 0:
                        _conn.execute(
                            "INSERT INTO tasks (id, title, body, assignee, status, priority, "
                            "created_by, created_at, workspace_kind) "
                            "VALUES (?, ?, ?, NULL, 'ready', 1, 'watchdog', ?, 'scratch')",
                            (_tid, _title, _body, _now)
                        )
                        _conn.commit()
                        fixes.append(f"Escalated {_desc} to kanban task {_tid}")
                    _conn.close()
                except Exception as _ce:
                    fixes.append(f"Escalation task creation failed for {_desc}: {_ce}")

        # Save state
        with open(_ws_path + ".tmp", "w") as _f:
            json.dump(_ws, _f, indent=2)
        os.replace(_ws_path + ".tmp", _ws_path)
    except Exception as _e:
        issues.append(f"Persistent-issue escalation error: {_e}")

    # Report
    if issues:
        print(f"WATCHDOG ALERT — {len(issues)} issue(s):")
        for issue in issues:
            print(f"  ⚠ {issue}")
        for fix in fixes:
            print(f"  ✓ {fix}")
    else:
        # Silent when healthy — no output
        pass


if __name__ == "__main__":
    main()
