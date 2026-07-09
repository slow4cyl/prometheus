#!/usr/bin/env python3
"""fleet_policy_stamper.py — sidecar that replaces three kanban_db FORK features by
stamping upstream-native columns in kanban.db, plus one provenance reconcile pass.

De-fork plan (~/.hermes/docs/defork-plan.md, section "Route: sidecar"); architecture
map ~/.hermes/docs/architecture-map.md (kanban dispatcher / sidecar stamping pattern —
a1_router.py is the reference pre-stamp implementation this mirrors).

Fork features replaced:
  Pass 1  "kanban_db — Fleet-wide goal loop (bounded finalize budget) via env"
          [plan: CONFIRMED] — stamp upstream's own per-task columns:
          tasks.goal_mode=1, goal_max_turns=COALESCE(goal_max_turns, 6) on ready
          rows. Upstream spawn converts these into HERMES_KANBAN_GOAL_MODE /
          HERMES_KANBAN_GOAL_MAX_TURNS (merge-base 7e8f50a kanban_db.py:7737-7739).
  Pass 2  "kanban_db — Built-in kanban-worker skill auto-injection with
          real-resolver probe" [plan: CORRECTED -> Partial] — append
          "kanban-worker" to tasks.skills (upstream JSON-array column,
          kanban_db.py:1139/:1755; emitted per-entry as --skills at :7788) on
          ready rows missing it, ONLY if the skill actually resolves. The probe
          delegates to the SAME resolver the spawn uses
          (agent.skill_commands.build_preloaded_skills_prompt), exactly like the
          fork's _kanban_worker_skill_available (fork kanban_db.py:7434) — a
          directory glob can never reliably mirror the loader, and preloading an
          unresolvable skill is FATAL at worker CLI startup. Probe runs in a
          subprocess per distinct assignee HERMES_HOME, once per tick.
          KNOWN LIMIT (documented in the plan row): INSERT->claim race means
          best-effort coverage, and review-lane workers get their skills
          overwritten post-claim — out of sidecar reach.
  Pass 3  "kanban_db — Per-profile-cap overflow reassignment to installed
          profiles only" [plan: CONFIRMED] — ready rows whose assignee is not an
          installed profile (phantom — the 60k-phantom-assignee pathology), or
          whose assignee is at/over kanban.max_in_progress_per_profile, are
          reassigned to 'default' with a 'reassigned' task_event (mirrors
          task_janitor_v2.normalize_orphaned_assignees + the existing
          per_profile_cap_reassignment event payload convention).
  Pass 4  "kanban_db — HERMES_MODEL provenance pin for worker results"
          [plan: CONFIRMED; support pass complementing
          ~/.hermes/scripts/write_worker_result.py's write-time resolution] —
          backfill prometheus.db worker_results.model IS NULL rows by joining
          kanban tasks.model_override on kanban_task_id (post-hoc repair class,
          like task_janitor_v2).

Safety contract:
  * flock singleton (~/.hermes/.fleet_policy_stamper.lock — peer convention);
  * WAL-safe short transactions (busy_timeout=30000, one bounded transaction per
    pass), guarded UPDATEs that re-check the full predicate at write time
    (a1_router.py idiom) so concurrent claim/status transitions are never
    clobbered;
  * never touches task rows outside status='ready' (pass 4 only writes
    worker_results.model on rows where it IS NULL; task rows are only read);
  * idempotent — a second run is a no-op; silent on stdout when noop;
  * a task_events audit row for every mutation (kinds: goal_mode_stamped,
    skills_injected, reassigned, model_provenance_backfilled);
  * fail open — schema drift (sentinel preflight below) or a failed skill probe
    skips the affected pass, writes ~/.hermes/PATCH_FAILED_fleet_policy_stamper,
    logs loudly to stderr, and leaves base behavior intact. Never crashes the
    fleet: a dead/failing sidecar just means upstream default behavior.

Sentinel guard: before any pass mutates, the expected upstream schema is
fingerprinted via PRAGMA table_info — the exact columns the merge-base
(7e8f50a) dispatcher reads (goal_mode/goal_max_turns/skills/assignee/
model_override on tasks; task_events(task_id,run_id,kind,payload,created_at);
worker_results(id,experiment_id,kanban_task_id,model)). Any mismatch = do not
stamp that pass.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

STAMPER = "fleet_policy_stamper"
KANBAN_WORKER_SKILL = "kanban-worker"
DEFAULT_GOAL_MAX_TURNS = 6
DEFAULT_PER_PROFILE_CAP = 20
DEFAULT_BACKFILL_LIMIT = 2000
TERMINAL_STATES = ("done", "archived")  # never written; passes 1-3 filter status='ready'

# Sentinel fingerprints: upstream columns each pass depends on.
SENTINEL_TASKS_COLS = {
    "id", "title", "status", "assignee", "skills",
    "model_override", "goal_mode", "goal_max_turns",
}
SENTINEL_EVENTS_COLS = {"task_id", "run_id", "kind", "payload", "created_at"}
SENTINEL_WR_COLS = {"id", "experiment_id", "kanban_task_id", "model"}


def default_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def default_kanban_db() -> Path:
    return Path(os.environ.get("HERMES_KANBAN_DB") or default_home() / "kanban.db").expanduser()


def default_prometheus_db() -> Path:
    return Path(os.environ.get("HERMES_PROMETHEUS_DB") or default_home() / "prometheus.db").expanduser()


def connect(db_path: Path) -> sqlite3.Connection:
    # a1_router.py connection idiom: long busy_timeout is the WAL-contention belt
    # (db_retry.py rationale), short transactions are the suspenders.
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def append_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict) -> None:
    # Exact a1_router.py convention: run_id NULL, epoch-int created_at.
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
        (task_id, kind, json.dumps(payload, ensure_ascii=False), int(time.time())),
    )


def table_cols(conn: sqlite3.Connection, table: str, schema: str = "main") -> set:
    try:
        rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {r["name"] for r in rows}


class Sentinel:
    """Fail-open schema guard. On mismatch: mark, log, skip the pass."""

    def __init__(self, marker_path: Path):
        self.marker_path = marker_path
        self.failures: list[str] = []

    def check(self, conn: sqlite3.Connection, table: str, expected: set, schema: str = "main") -> bool:
        have = table_cols(conn, table, schema=schema)
        missing = expected - have
        if not missing:
            return True
        detail = f"{schema}.{table} missing expected upstream columns: {sorted(missing)} (have {len(have)} cols)"
        self.failures.append(detail)
        print(f"{STAMPER}: SENTINEL MISMATCH — {detail} — pass skipped, base behavior intact",
              file=sys.stderr)
        return False

    def flush_marker(self) -> None:
        if not self.failures:
            return
        try:
            self.marker_path.write_text(
                json.dumps(
                    {
                        "script": STAMPER,
                        "when": int(time.time()),
                        "failures": self.failures,
                        "note": "sentinel mismatch: pass(es) skipped fail-open; no rows were stamped by the failed pass(es)",
                    },
                    indent=2,
                )
                + "\n"
            )
        except OSError as exc:
            print(f"{STAMPER}: could not write marker {self.marker_path}: {exc}", file=sys.stderr)


def load_per_profile_cap(config_path: Path) -> int:
    """kanban.max_in_progress_per_profile from config.yaml (read-only), default 20."""
    try:
        import yaml  # available on this deployment

        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        val = ((cfg.get("kanban") or {}).get("max_in_progress_per_profile"))
        if isinstance(val, int) and val > 0:
            return val
    except Exception as exc:  # fail open to default
        print(f"{STAMPER}: config cap read failed ({exc}); using default {DEFAULT_PER_PROFILE_CAP}",
              file=sys.stderr)
    return DEFAULT_PER_PROFILE_CAP


def installed_profiles(profiles_dir: Path) -> set:
    """'default' + installed profile dirs, lowercased (task_janitor_v2 idiom)."""
    valid = {"default"}
    try:
        if profiles_dir.is_dir():
            valid |= {d.name.lower() for d in profiles_dir.iterdir() if d.is_dir()}
    except OSError:
        pass
    return valid


def profile_home(assignee: str, hermes_home: Path, profiles_dir: Path) -> Path:
    """HERMES_HOME the spawned worker for this assignee will run under
    (per-profile config is a full HERMES_HOME override)."""
    a = (assignee or "").strip().lower()
    if a and a != "default" and (profiles_dir / a).is_dir():
        return profiles_dir / a
    return hermes_home


def probe_skill_resolves(home: Path, repo: Path, probe_mode: str, cache: dict, timeout: float = 45.0) -> bool:
    """True iff 'kanban-worker' resolves under `home`, using the SAME resolver the
    spawn uses (mirrors fork _kanban_worker_skill_available: a glob heuristic can
    never reliably mirror the loader). Subprocess-isolated so per-home probes
    cannot poison each other's import-time HERMES_HOME caches. Fail -> False
    (fail open: never stamp a skill that might abort the worker at startup)."""
    if probe_mode == "assume-ok":
        return True
    if probe_mode == "assume-missing":
        return False
    key = str(home)
    if key in cache:
        return cache[key]
    ok = False
    code = (
        "import sys, os\n"
        f"os.environ['HERMES_HOME'] = {str(home)!r}\n"
        f"sys.path.insert(0, {str(repo)!r})\n"
        "from agent.skill_commands import build_preloaded_skills_prompt\n"
        "_p, loaded, missing = build_preloaded_skills_prompt(\n"
        f"    [{KANBAN_WORKER_SKILL!r}], task_id='_fleet_policy_stamper_probe')\n"
        "sys.exit(0 if (loaded and not missing) else 3)\n"
    )
    try:
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, timeout=timeout, cwd=str(home),
        )
        ok = res.returncode == 0
        if not ok:
            print(f"{STAMPER}: skill probe negative for home={home} rc={res.returncode} "
                  f"stderr={res.stderr.decode('utf-8', 'replace')[-300:]!r}", file=sys.stderr)
    except Exception as exc:
        print(f"{STAMPER}: skill probe error for home={home}: {exc}", file=sys.stderr)
        ok = False
    cache[key] = ok
    return ok


# ---------------------------------------------------------------------------
# Pass 1 — fleet-wide goal loop (goal_mode/goal_max_turns stamp)
# ---------------------------------------------------------------------------

def pass_goal_mode(conn: sqlite3.Connection, args, counters: dict) -> None:
    c = counters["goal_mode"]
    rows = conn.execute(
        "SELECT id, goal_max_turns FROM tasks "
        "WHERE status = 'ready' AND (goal_mode IS NULL OR goal_mode = 0)"
    ).fetchall()
    c["candidates"] = len(rows)
    if not rows:
        return
    with conn:  # one short transaction
        for row in rows:
            tid = str(row["id"])
            turns_after = row["goal_max_turns"] if row["goal_max_turns"] else args.goal_max_turns
            if args.dry_run:
                c["stamped"] += 1
                print(f"  [dry-run] goal_mode {tid}: goal_mode=1 goal_max_turns={turns_after}")
                continue
            cur = conn.execute(
                "UPDATE tasks SET goal_mode = 1, goal_max_turns = COALESCE(goal_max_turns, ?) "
                "WHERE id = ? AND status = 'ready' AND (goal_mode IS NULL OR goal_mode = 0)",
                (args.goal_max_turns, tid),
            )
            if cur.rowcount:
                c["stamped"] += 1
                append_event(conn, tid, "goal_mode_stamped", {
                    "goal_mode": 1,
                    "goal_max_turns": turns_after,
                    "reason": "fleet_goal_loop_policy",
                    "by": STAMPER,
                })
            else:
                c["raced"] += 1


# ---------------------------------------------------------------------------
# Pass 2 — kanban-worker skill injection (real-resolver probe)
# ---------------------------------------------------------------------------

def pass_skills(conn: sqlite3.Connection, args, counters: dict, probe_cache: dict) -> None:
    c = counters["skills"]
    rows = conn.execute(
        "SELECT id, assignee, skills FROM tasks "
        "WHERE status = 'ready' AND (skills IS NULL OR skills NOT LIKE ?)",
        (f'%"{KANBAN_WORKER_SKILL}"%',),
    ).fetchall()
    c["candidates"] = len(rows)
    if not rows:
        return
    hermes_home = Path(args.hermes_home).expanduser()
    profiles_dir = Path(args.profiles_dir).expanduser()
    repo = Path(args.repo).expanduser()
    with conn:
        for row in rows:
            tid = str(row["id"])
            raw = row["skills"]
            # Upstream stores skills as a JSON array of names (kanban_db.py:878-881).
            # NULL = defaults; unparseable blobs are left alone (upstream treats them
            # as None at read time — clobbering could destroy operator intent).
            skills_list: list = []
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        skills_list = [str(s) for s in parsed if s]
                    else:
                        c["skipped_unparseable"] += 1
                        continue
                except (ValueError, TypeError):
                    c["skipped_unparseable"] += 1
                    continue
            if KANBAN_WORKER_SKILL in skills_list:
                c["already_present"] += 1  # LIKE prefilter false positive
                continue
            home = profile_home(row["assignee"], hermes_home, profiles_dir)
            if not probe_skill_resolves(home, repo, args.probe_mode, probe_cache):
                c["skipped_probe_failed"] += 1
                continue
            new_skills = json.dumps(skills_list + [KANBAN_WORKER_SKILL], ensure_ascii=False)
            if args.dry_run:
                c["stamped"] += 1
                print(f"  [dry-run] skills {tid}: {raw!r} -> {new_skills!r}")
                continue
            # Guarded: `skills IS ?` also matches NULL, so a concurrent edit or
            # claim (status change) makes this a no-op instead of a clobber.
            cur = conn.execute(
                "UPDATE tasks SET skills = ? WHERE id = ? AND status = 'ready' AND skills IS ?",
                (new_skills, tid, raw),
            )
            if cur.rowcount:
                c["stamped"] += 1
                append_event(conn, tid, "skills_injected", {
                    "added": KANBAN_WORKER_SKILL,
                    "skills_after": skills_list + [KANBAN_WORKER_SKILL],
                    "probe_home": str(home),
                    "by": STAMPER,
                })
            else:
                c["raced"] += 1


# ---------------------------------------------------------------------------
# Pass 3 — phantom-assignee + per-profile-cap overflow reassignment
# ---------------------------------------------------------------------------

def pass_reassign(conn: sqlite3.Connection, args, counters: dict) -> None:
    c = counters["reassign"]
    valid = installed_profiles(Path(args.profiles_dir).expanduser())
    cap = args.cap
    running: dict = {}
    for r in conn.execute(
        "SELECT lower(coalesce(assignee,'')) AS a, COUNT(*) AS n FROM tasks "
        "WHERE status = 'running' GROUP BY 1"
    ).fetchall():
        running[r["a"]] = int(r["n"])
    default_has_headroom = running.get("default", 0) < cap

    rows = conn.execute(
        "SELECT id, assignee, title FROM tasks "
        "WHERE status = 'ready' AND assignee IS NOT NULL AND trim(assignee) <> '' "
        "AND lower(trim(assignee)) <> 'default'"
    ).fetchall()
    c["candidates"] = len(rows)
    if not rows:
        return
    with conn:
        for row in rows:
            tid = str(row["id"])
            assignee_raw = row["assignee"]
            a = (assignee_raw or "").strip().lower()
            if a not in valid:
                reason = "phantom_assignee_reassignment"
                ckey = "phantom_reassigned"
            elif running.get(a, 0) >= cap:
                if not default_has_headroom:
                    # No point bouncing onto an equally-capped default; the
                    # dispatcher defers either way. Retry next tick.
                    c["skipped_default_capped"] += 1
                    continue
                reason = "per_profile_cap_reassignment"
                ckey = "overflow_reassigned"
            else:
                c["assignee_ok"] += 1
                continue
            if args.dry_run:
                c[ckey] += 1
                print(f"  [dry-run] reassign {tid}: {assignee_raw!r} -> 'default' ({reason})")
                continue
            cur = conn.execute(
                "UPDATE tasks SET assignee = 'default' "
                "WHERE id = ? AND status = 'ready' AND assignee IS ?",
                (tid, assignee_raw),
            )
            if cur.rowcount:
                c[ckey] += 1
                # Existing 'reassigned' payload convention: {"from","to","reason"}.
                append_event(conn, tid, "reassigned", {
                    "from": assignee_raw,
                    "to": "default",
                    "reason": reason,
                    "by": STAMPER,
                })
            else:
                c["raced"] += 1


# ---------------------------------------------------------------------------
# Pass 4 — worker_results.model provenance backfill (prometheus.db)
# ---------------------------------------------------------------------------

def pass_provenance_backfill(conn: sqlite3.Connection, args, counters: dict, sentinel: Sentinel) -> None:
    """conn is the kanban connection; prometheus.db is ATTACHed as `prom` so the
    worker_results UPDATE and its task_events audit row commit together. Task
    rows are only READ here (terminal-state tasks are legitimate join targets —
    provenance concerns finished work — but are never written)."""
    c = counters["provenance"]
    prom_path = Path(args.prometheus_db).expanduser()
    if not prom_path.exists():
        c["skipped_no_prometheus_db"] = 1
        print(f"{STAMPER}: prometheus db not found at {prom_path}; pass skipped", file=sys.stderr)
        return
    conn.execute("ATTACH DATABASE ? AS prom", (str(prom_path),))
    try:
        if not sentinel.check(conn, "worker_results", SENTINEL_WR_COLS, schema="prom"):
            return
        rows = conn.execute(
            """
            SELECT wr.id AS wr_id, wr.experiment_id AS exp, wr.kanban_task_id AS tid,
                   t.model_override AS model
            FROM prom.worker_results wr
            JOIN main.tasks t ON t.id = wr.kanban_task_id
            WHERE wr.model IS NULL
              AND t.model_override IS NOT NULL AND trim(t.model_override) <> ''
            LIMIT ?
            """,
            (args.backfill_limit,),
        ).fetchall()
        c["candidates"] = len(rows)
        if not rows:
            return
        with conn:
            for row in rows:
                if args.dry_run:
                    c["backfilled"] += 1
                    if c["backfilled"] <= 10:
                        print(f"  [dry-run] provenance wr_id={row['wr_id']} exp={row['exp']} "
                              f"task={row['tid']} model={row['model']!r}")
                    continue
                cur = conn.execute(
                    "UPDATE prom.worker_results SET model = ? WHERE id = ? AND model IS NULL",
                    (row["model"], row["wr_id"]),
                )
                if cur.rowcount:
                    c["backfilled"] += 1
                    append_event(conn, str(row["tid"]), "model_provenance_backfilled", {
                        "experiment_id": row["exp"],
                        "worker_result_id": row["wr_id"],
                        "model": row["model"],
                        "by": STAMPER,
                    })
                else:
                    c["raced"] += 1
        if args.dry_run and c["backfilled"] > 10:
            print(f"  [dry-run] ... and {c['backfilled'] - 10} more provenance backfills")
    finally:
        try:
            conn.execute("DETACH DATABASE prom")
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------------------

def fresh_counters() -> dict:
    from collections import defaultdict
    return {
        "goal_mode": defaultdict(int),
        "skills": defaultdict(int),
        "reassign": defaultdict(int),
        "provenance": defaultdict(int),
    }


def any_activity(counters: dict) -> bool:
    # Only actual mutations (or races, which are transient) break silence.
    # Persistent skip conditions (unparseable skills blob, capped default,
    # probe-negative) would otherwise print every tick forever; they are
    # visible in the status file, via --verbose, and (for probe/sentinel
    # problems) as stderr warnings.
    interesting = ("stamped", "phantom_reassigned", "overflow_reassigned", "backfilled", "raced")
    return any(counters[p][k] for p in counters for k in interesting if k in counters[p])


def write_status(args, counters: dict, sentinel: Sentinel, started: float) -> None:
    """Liveness breadcrumb for the watchdog class of checks (the plan's named risk
    for this sidecar is 'silently dead sidecar reverts the fleet'). Best-effort."""
    try:
        status_path = Path(args.status_file).expanduser()
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps({
            "when": int(time.time()),
            "duration_s": round(time.time() - started, 3),
            "mode": "dry_run" if args.dry_run else "live",
            "db": str(args.db),
            "prometheus_db": str(args.prometheus_db),
            "counters": {p: dict(v) for p, v in counters.items()},
            "sentinel_failures": sentinel.failures,
        }, indent=2) + "\n")
    except OSError:
        pass


def main() -> int:
    home = default_home()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(default_kanban_db()),
                    help="kanban.db path (or env HERMES_KANBAN_DB)")
    ap.add_argument("--prometheus-db", default=str(default_prometheus_db()),
                    help="prometheus.db path (or env HERMES_PROMETHEUS_DB)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be stamped; no writes")
    ap.add_argument("--verbose", action="store_true", help="print counters even when noop")
    ap.add_argument("--goal-max-turns", type=int, default=DEFAULT_GOAL_MAX_TURNS)
    ap.add_argument("--cap", type=int, default=None,
                    help="per-profile running cap (default: kanban.max_in_progress_per_profile from config.yaml, else 20)")
    ap.add_argument("--config", default=str(home / "config.yaml"), help="config.yaml (read-only, for --cap default)")
    ap.add_argument("--profiles-dir", default=str(home / "profiles"))
    ap.add_argument("--hermes-home", default=str(home))
    ap.add_argument("--repo", default=str(home / "hermes-agent"),
                    help="hermes checkout for the skill-resolver probe (read-only import)")
    ap.add_argument("--probe-mode", choices=("auto", "assume-ok", "assume-missing"), default="auto",
                    help="skill-resolver probe override (test hook; auto = real resolver in a subprocess)")
    ap.add_argument("--backfill-limit", type=int, default=DEFAULT_BACKFILL_LIMIT,
                    help="max worker_results provenance backfills per run")
    ap.add_argument("--lock", default=str(home / f".{STAMPER}.lock"))
    ap.add_argument("--marker", default=str(home / f"PATCH_FAILED_{STAMPER}"))
    ap.add_argument("--status-file", default=str(home / "logs" / f"{STAMPER}.status.json"))
    args = ap.parse_args()

    # flock singleton (task_janitor / peer convention): overlapping cron ticks exit quietly.
    lock_path = Path(args.lock).expanduser()
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    started = time.time()
    counters = fresh_counters()
    sentinel = Sentinel(Path(args.marker).expanduser())
    if args.cap is None:
        args.cap = load_per_profile_cap(Path(args.config).expanduser())

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"{STAMPER}: kanban db not found: {db_path}", file=sys.stderr)
        return 1

    conn = connect(db_path)
    try:
        tasks_ok = sentinel.check(conn, "tasks", SENTINEL_TASKS_COLS)
        events_ok = sentinel.check(conn, "task_events", SENTINEL_EVENTS_COLS)
        if tasks_ok and events_ok:
            probe_cache: dict = {}
            # Reassign first so the skill probe sees each row's FINAL assignee.
            pass_reassign(conn, args, counters)
            pass_goal_mode(conn, args, counters)
            pass_skills(conn, args, counters, probe_cache)
            pass_provenance_backfill(conn, args, counters, sentinel)
    finally:
        conn.close()

    sentinel.flush_marker()
    write_status(args, counters, sentinel, started)

    if args.dry_run or args.verbose or any_activity(counters) or sentinel.failures:
        summary = {p: dict(v) for p, v in counters.items()}
        mode = "dry_run" if args.dry_run else "live"
        print(f"{STAMPER} mode={mode} cap={args.cap} {json.dumps(summary, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
