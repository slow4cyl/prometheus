#!/usr/bin/env python3
"""Capacity-gated local-first router for Agents-A1 Kanban workers.

This script only chooses whether an eligible ready task should use the local
Agents-A1 vLLM endpoint. It deliberately does not tune generation budgets; any
provider request-building bug belongs in Hermes core/provider logic, not here.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

A1_MODEL = "agents-a1"
DEFAULT_BASE_URL = "http://localhost:8001/v1"
DEFAULT_CAP = 6  # matches the live FP4 serving shape (6x96K decode slots since the 2026-07-08 OOM reshape)
DEFAULT_INTERVAL = 10.0

DENY_TAGS = (
    "[ADVERSARIAL]",
    "[RE-EXAMINE]",
    "[ARBITRATION]",
    "[RECOMPUTE]",
    "[CLEAN-ROOM]",
    "[WORLD]",
    "[FLOOD]",
    "[CANDIDATE-RETEST]",
    "[BOUNDARY]",
    "[COMPRESSION-BOUNDARY]",
    "[INJECTION]",
    "[SUPPORTED]",
    "[REFUTED]",
)
DENY_PREFIXES = (
    "exp_adv_",
    "exp_arb_",
    "exp_cleanroom",
    "exp_world",
    "exp_cross",
    "exp_qi_",
)
DENY_PHRASES = (
    "break claim",
    "settle claim",
    "blind replication",
    "ground claim #",
)


def default_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def default_db() -> Path:
    return Path(os.environ.get("HERMES_KANBAN_DB") or default_home() / "kanban.db").expanduser()


def default_log() -> Path:
    return default_home() / "logs" / "a1_router.log"


def default_kill_switch() -> Path:
    return default_home() / "a1_router.OFF"


def setup_logging(log_path: Path, verbose: bool = False) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def health_url_for(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def a1_healthy(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    url = health_url_for(base_url)
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2_000_000)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return False, f"health_error:{type(exc).__name__}:{exc}"
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        return False, f"health_bad_json:{type(exc).__name__}"
    ids = {
        str(item.get("id"))
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }
    if model not in ids:
        return False, f"model_missing:{model}"
    return True, "ok"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def append_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
        (task_id, kind, json.dumps(payload, ensure_ascii=False), int(time.time())),
    )


def text_blob(row: sqlite3.Row) -> tuple[str, str, str]:
    title = str(row["title"] or "")
    body = str(row["body"] or "")
    return title, body, f"{title}\n{body}"


def deny_reason(row: sqlite3.Row) -> str | None:
    title, _body, blob = text_blob(row)
    upper = blob.upper()
    lower_title = title.strip().lower()
    lower_blob = blob.lower()
    for tag in DENY_TAGS:
        if tag in upper:
            return f"deny_tag:{tag}"
    for prefix in DENY_PREFIXES:
        if lower_title.startswith(prefix):
            return f"deny_prefix:{prefix}"
    for phrase in DENY_PHRASES:
        if phrase in lower_blob:
            return f"deny_phrase:{phrase}"
    return None


def allow_reason(row: sqlite3.Row) -> str | None:
    title, _body, _blob = text_blob(row)
    stripped = title.strip()
    if stripped.upper().startswith("[TRANSFER"):
        return "allow_transfer"
    if re.match(r"^exp_[0-9]", stripped.lower()):
        return "allow_digit_exp"
    return None


def eligible_reason(row: sqlite3.Row) -> tuple[bool, str]:
    denied = deny_reason(row)
    if denied:
        return False, denied
    allowed = allow_reason(row)
    if allowed:
        return True, allowed
    return False, "not_exploratory_lane"


def clear_failed_a1_pins(conn: sqlite3.Connection, *, live: bool) -> set[str]:
    rows = conn.execute(
        """
        SELECT id, title, body, consecutive_failures, last_failure_error
        FROM tasks
        WHERE model_override = ?
          AND status IN ('ready', 'todo')
          AND consecutive_failures > 0
        ORDER BY created_at ASC
        """,
        (A1_MODEL,),
    ).fetchall()
    cleared_ids: set[str] = set()
    for row in rows:
        # Only clear pins the ROUTER set (the exploratory transfer/digit-exp lane).
        # A failed task that is itself a critical-lane task (adversarial, etc.) was
        # pinned to A1 DELIBERATELY by its own enqueuer as a cross-family attacker;
        # unstamping it would demote the attack to the mimo default = same-family.
        # Leave those to their own expire/re-enqueue rotation.
        if deny_reason(row) is not None:
            continue
        cleared_ids.add(str(row["id"]))
        payload = {
            "model": A1_MODEL,
            "reason": "local_a1_failed_before_completion",
            "consecutive_failures": row["consecutive_failures"],
            "last_failure_error": row["last_failure_error"],
        }
        logging.info("clear_a1_pin task=%s failures=%s", row["id"], row["consecutive_failures"])
        if live:
            conn.execute(
                "UPDATE tasks SET model_override = NULL WHERE id = ? AND model_override = ?",
                (row["id"], A1_MODEL),
            )
            append_event(conn, row["id"], "a1_route_cleared", payload)
    return cleared_ids


def current_a1_usage(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM tasks
        WHERE model_override = ?
          AND status IN ('ready', 'running')
        """,
        (A1_MODEL,),
    ).fetchone()
    return int(row["n"] if row else 0)


def candidate_rows(conn: sqlite3.Connection, assignee: str, scan_limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, title, body, priority, created_at, assignee
        FROM tasks
        WHERE status = 'ready'
          AND (model_override IS NULL OR trim(model_override) = '')
          AND coalesce(assignee, '') IN ('', ?)
        ORDER BY priority DESC, created_at ASC
        LIMIT ?
        """,
        (assignee, int(scan_limit)),
    ).fetchall()


def route_once(args: argparse.Namespace) -> dict:
    live = bool(args.live)
    db_path = Path(args.db).expanduser()
    kill_switch = Path(args.kill_switch).expanduser()

    if kill_switch.exists():
        result = {"ok": True, "mode": "live" if live else "dry_run", "action": "disabled", "kill_switch": str(kill_switch)}
        logging.info("router_disabled kill_switch=%s", kill_switch)
        return result

    healthy = True
    health_reason = "skipped"
    if not args.skip_health:
        healthy, health_reason = a1_healthy(args.base_url, args.model, args.health_timeout)
    if not healthy:
        result = {"ok": True, "mode": "live" if live else "dry_run", "action": "health_down", "health": health_reason}
        logging.info("health_down reason=%s", health_reason)
        return result

    conn = connect(db_path)
    try:
        with conn:
            cleared_ids = clear_failed_a1_pins(conn, live=live)
            cleared = len(cleared_ids)
            used = current_a1_usage(conn)
            free = max(0, int(args.cap) - used)
            if free <= 0:
                result = {
                    "ok": True,
                    "mode": "live" if live else "dry_run",
                    "action": "capacity_full",
                    "cap": int(args.cap),
                    "used": used,
                    "free": 0,
                    "cleared": cleared,
                    "health": health_reason,
                }
                logging.info("capacity_full cap=%s used=%s cleared=%s", args.cap, used, cleared)
                return result

            selected: list[dict] = []
            denied_seen = 0
            for row in candidate_rows(conn, args.assignee, args.scan_limit):
                if str(row["id"]) in cleared_ids:
                    denied_seen += 1
                    logging.debug("skip_task task=%s reason=cleared_failed_a1_pin_this_tick", row["id"])
                    continue
                eligible, reason = eligible_reason(row)
                if not eligible:
                    denied_seen += 1
                    logging.debug("skip_task task=%s reason=%s", row["id"], reason)
                    continue
                selected.append({"id": row["id"], "reason": reason, "title": row["title"]})
                if len(selected) >= free:
                    break

            for item in selected:
                logging.info(
                    "route_task task=%s provider=custom:agents-a1 model=%s used=%s free_before=%s reason=%s live=%s",
                    item["id"], args.model, used, free, item["reason"], live,
                )
                if live:
                    cur = conn.execute(
                        """
                        UPDATE tasks
                        SET model_override = ?
                        WHERE id = ?
                          AND status = 'ready'
                          AND (model_override IS NULL OR trim(model_override) = '')
                        """,
                        (args.model, item["id"]),
                    )
                    if cur.rowcount:
                        append_event(
                            conn,
                            item["id"],
                            "a1_route_selected",
                            {
                                "provider": "custom:agents-a1",
                                "model": args.model,
                                "reason": item["reason"],
                                "cap": int(args.cap),
                                "used_before": used,
                                "free_before": free,
                            },
                        )

            result = {
                "ok": True,
                "mode": "live" if live else "dry_run",
                "action": "routed" if selected else "no_eligible_tasks",
                "cap": int(args.cap),
                "used": used,
                "free": free,
                "selected": selected,
                "denied_seen": denied_seen,
                "cleared": cleared,
                "health": health_reason,
            }
            logging.info("tick_result %s", json.dumps(result, ensure_ascii=False))
            return result
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Capacity-gated Agents-A1 Kanban router")
    p.add_argument("--db", default=str(default_db()))
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default=A1_MODEL)
    p.add_argument("--cap", type=int, default=int(os.environ.get("A1_CAP", DEFAULT_CAP)))
    p.add_argument("--assignee", default=os.environ.get("A1_ROUTER_ASSIGNEE", "default"))
    p.add_argument("--scan-limit", type=int, default=200)
    p.add_argument("--health-timeout", type=float, default=2.0)
    p.add_argument("--skip-health", action="store_true", help="Testing only: do not query the local endpoint")
    p.add_argument("--kill-switch", default=str(default_kill_switch()))
    p.add_argument("--log", default=str(default_log()))
    p.add_argument("--live", action="store_true", help="Apply updates. Default is dry-run.")
    p.add_argument("--once", action="store_true", help="Run one tick and exit")
    p.add_argument("--interval", type=float, default=0.0, help="Loop interval seconds. 0 means one tick.")
    p.add_argument("--json", action="store_true", help="Print final tick result JSON")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.cap < 1:
        parser.error("--cap must be >= 1")
    setup_logging(Path(args.log).expanduser(), verbose=args.verbose)

    last = None
    if args.once or args.interval <= 0:
        last = route_once(args)
        if args.json:
            print(json.dumps(last, ensure_ascii=False, sort_keys=True))
        return 0

    logging.info("starting loop interval=%s live=%s cap=%s db=%s", args.interval, args.live, args.cap, args.db)
    while True:
        last = route_once(args)
        if args.json:
            print(json.dumps(last, ensure_ascii=False, sort_keys=True), flush=True)
        time.sleep(float(args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
