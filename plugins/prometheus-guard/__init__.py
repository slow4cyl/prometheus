"""prometheus-guard plugin — reject bogus kanban_block calls from fleet workers.

Port of the prometheus-fork in-file guards in ``tools/kanban_tools.py``
(fork commit e6004d504 plus the 2026-06-26 deferral-class patterns) into a
user plugin, so the guard survives hermes-agent base updates with zero
rebase surface.

Three guards, all firing on the ``pre_tool_call`` hook — two for
``kanban_block`` and one for ``kanban_complete``:

1. **Tool-hallucination guard** — workers claiming "no terminal / only
   kanban tools / can't run write_worker_result" when those tools ARE in
   the toolset. Claims are verified against the runtime tool list before
   rejecting, so a worker genuinely dispatched without terminal is never
   forced.
2. **Uncertainty-deferral guard** — workers blocking with "awaiting human
   review" when worker_results are already written for the task, or when
   the task body already contains the exact write_worker_result.py
   command to run. Gated on ``HERMES_KANBAN_TASK`` so it only enforces
   for dispatcher-spawned workers.
3. **Worker-result-written completion gate** — ``kanban_complete`` on an
   experiment-class task (``exp_`` prefix / ``[TRANSFER]`` / ``[STRANDED``
   in the title) is blocked unless a matching ``worker_results`` row
   exists in ``~/.hermes/prometheus.db`` (port of the fork's
   ``_enforce_worker_result_written``, tools/kanban_tools.py:470-560).
   Gated on ``HERMES_KANBAN_TASK``; every failure path fails OPEN.

A rejected block returns ``{"error": "BLOCK REJECTED — ..."}`` to the
model — the same shape ``tools/registry.tool_error`` produces — so worker
behavior is identical to the in-file guard. The in-file guard remains
during the overlap period; this plugin fires first (``pre_tool_call``
runs before the tool executes), making the in-file copy redundant but
harmless. Retire fork commit e6004d504 at the next base update.

DB discipline: both DB reads (worker_results existence, task body) are
short-lived ``mode=ro`` connections — the guard can never hold a write
lock on a live WAL database.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool-hallucination patterns — verbatim from tools/kanban_tools.py
# ---------------------------------------------------------------------------

_TOOL_HALLUCINATION_PATTERNS = [
    # Direct "no terminal" claims
    r"no\s+terminal",
    r"terminal\s+(tool\s+)?is\s+not\s+(available|present|enabled)",
    r"terminal\s+access\s+(is\s+)?unavailable",
    r"terminal\s+access\s+(?:is\s+)?(?:unavailable|not\s+available|missing)",
    r"cannot\s+(access|use|run|execute|call)\s+(the\s+)?terminal",
    r"without\s+terminal\s+access",
    r"lacks?\s+terminal",
    r"missing\s+terminal",
    r"need\s+terminal",
    # Shell/exec claims
    r"no\s+shell\s+(tool|access|capability)",
    r"shell\s+(tool\s+)?is\s+not\s+(available|present)",
    r"cannot\s+(execute|run)\s+(shell|bash|commands?)",
    r"no\s+(bash|shell|exec)\s+(access|tool|capability)",
    # write_worker_result deferral
    r"write_worker_result\.py.*(?:cannot|can't|unable|not\s+available|no\s+terminal|no\s+shell|requires?\s+shell)",
    r"(?:cannot|can't|unable)\s+.*write_worker_result",
    r"write_worker_result.*(?:must\s+be\s+run|needs?\s+to\s+be\s+(run|executed|called))",
    r"(?:parent|human|someone)\s+(agent\s+)?will\s+need\s+to\s+run.*write_worker_result",
    r"(?:parent|human|someone)\s+(agent\s+)?needs?\s+to\s+run.*write_worker_result",
    r"write_worker_result\.py\s+(?:must\s+be\s+)?(?:run|executed)\s+(?:manually|by\s+(?:human|parent))",
    # General tool-unavailability claims
    r"(?:web_?search|terminal|file|read_file|write_file)\s+(tool\s+)?is\s+not\s+(available|present|enabled)",
    r"(?:web_?search|terminal|file)\s+(?:access|tool)\s+(?:is\s+)?(?:unavailable|missing|not\s+available)",
    r"(?:web_?search|terminal|file|browser)\s+tools?\s+which\s+are\s+not\s+available",
    r"cannot\s+(complete|submit|finish).*write_worker_result",
    r"mandatory\s+.*write_worker_result.*(?:cannot|can't|unable|no\s+terminal|no\s+shell|not\s+available)",
    r"mandatory\s+(?:pre-)?completion\s+step.*(?:requires|not\s+available|cannot|unable)",
    r"pre-completion\s+step.*(?:requires|not\s+available|cannot|unable)",
    # "Only kanban tools" pattern
    r"only\s+(?:has?|have)\s+kanban[\s_-]*(?:lifecycle\s+)?tools",
    r"(?:only|just)\s+kanban\s+tools",
    r"no\s+(?:terminal|web|file|shell)\s+(?:tool|access|capability)\s+(?:is\s+)?(?:available|present|enabled)",
    # "kanban_* tools" pattern (with wildcard)
    r"only\s+(?:has?|have)\s+kanban[_\*]*\s+tools",
    r"kanban[_\*]*\s+tools\s+in\s+(?:my|the)\s+(?:available\s+)?toolset",
    # "do not have" patterns
    r"do\s+not\s+have\s+(?:terminal|web|file|shell|exec)",
    r"don'?t\s+have\s+(?:terminal|web|file|shell|exec)",
    r"lack\s+(?:the\s+)?(?:terminal|web|file|shell|exec)",
    # "file I/O" pattern
    r"file\s+I/O",
    r"file\s+io",
    # "not available in this session/environment"
    r"not\s+available\s+in\s+this\s+(?:session|environment|profile)",
    r"not\s+(?:available|present)\s+in\s+(?:my|the)\s+(?:toolset|function\s+schema)",
    # "capabilities not available"
    r"capabilities?\s+not\s+available",
    r"cannot\s+(?:execute|run|complete).*(?:in\s+this|current)\s+(?:session|environment)",
    # "requires capabilities not available"
    r"requires?\s+capabilities?\s+not\s+available",
    r"requires?\s+(?:script\s+)?execution\s+that\s+isn'?t\s+possible",
    # "cannot execute/run" with tool unavailability
    r"cannot\s+(?:execute|run)\s+.*(?:no|not|without|lack)",
    r"no\s+(?:terminal|web|file|shell)\s+tool\s+available",
    r"which\s+are\s+not\s+available",
    r"not\s+available\s+in\s+this\s+(?:agent|worker)",
    r"lack\s+the\s+capability\s+to\s+execute",
    # Euphemism patterns
    r"experiment\s+result\s+logging\s+script.*(?:requires?\s+shell|no\s+terminal|not\s+available)",
    r"result\s+(?:logging|recording)\s+(?:step|script|tool).*(?:requires?\s+shell|no\s+terminal|not\s+available)",
    # "requires web search/terminal" without acknowledging they exist
    r"requires?\s+(?:web_?search|terminal|browser)\s+(?:tools?\s+)?(?:which\s+)?(?:are\s+)?not\s+available",
    r"requires?\s+(?:web_?search|terminal|browser)\s+(?:to\s+)?(?:search|run|execute)",
    r"dispatched\s+without\s+(?:terminal|web|exec)\s+tools?",
    # "terminal function not in schema" — workers say "function" instead of "tool"
    r"terminal\s+(?:tool|function)\s+(?:is\s+)?not\s+in\s+(?:this\s+)?(?:worker|agent|my|the)\b",
    r"terminal\s+(?:tool|function)\s+is\s+not\s+(?:part\s+of\s+)?(?:this\s+)?(?:worker|agent|my|the)\b",
    r"(?:not\s+in|not\s+part\s+of)\s+(?:this\s+)?(?:worker|agent|my|the)\s+available\s+(?:function\s+)?schema",
    # "I need a human to run" — first person framing
    r"I\s+need\s+(?:a\s+)?human\s+to\s+run",
    r"I\s+need\s+(?:a\s+)?human\s+to\s+(?:execute|complete|finish|do)",
    # "need a human to run this command" — for write_worker_result specifically
    r"need\s+(?:a\s+)?human\s+to\s+run.*write_worker_result",
    # "only has/kanban_block/comment/..." — lists individual kanban tool names
    r"only\s+(?:kanban_block|kanban_complete|kanban_comment|kanban_heartbeat|kanban_show)",
    r"only\s+(?:has?|have)\s+.*(?:kanban_block|kanban_complete|kanban_comment)",
    # "only defined" with kanban tool names
    r"only\s+(?:kanban[_\*]*\w+\s*,?\s*)+are\s+defined",
    # "terminal function is not" — general function-not-tool variant
    r"terminal\s+(?:tool|function|command|capability)\s+(?:is\s+)?(?:not\s+)?(?:in\s+|available\s+|present\s+)",
    # "cannot run write_worker_result" without terminal
    r"(?:cannot|can't|unable|not\s+able\s+to)\s+(?:run|execute|call)\s+.*write_worker_result",
    # "need to write results" but deferring to human
    r"need.*(?:human|someone|parent).*to\s+(?:run|execute|write).*result",
    # "requires write_worker_result" with inability
    r"requires?.*write_worker_result.*(?:human|manual|intervention)",
    # ---- Deferral class (2026-06-26): worker finished the work and merely
    # NARRATES the result-writing command, or defers it to a child task,
    # instead of executing it — without claiming any tool is missing. These
    # are the dominant live block phrasings ("I need to run write_worker_result",
    # "waiting for child task to execute write_worker_result"). Scoped to
    # write_worker_result so genuine infra errors (e.g. "database is locked",
    # "script path needs verification") are NOT swept in.
    r"(?:i\s+)?need\s+to\s+(?:run|call|execute)\s*:?\s*`?\s*(?:python3?\s+)?[^\n]*write_worker_result",
    r"waiting\s+for\s+(?:a\s+)?(?:child|another|sub)\s*-?\s*task.*write_worker_result",
    r"will\s+complete\s+(?:once|after|when).*(?:result\s+is\s+written|written\s+to\s+the\s+database|write_worker_result)",
    r"(?:still\s+)?need\s+to\s+(?:run|call|execute)\s+`?write_worker_result",
]

# Map tool-name keywords in hallucination reasons to the actual tool names
# to check in the runtime tool list.
_TOOL_KEYWORD_MAP = {
    "terminal": ["terminal"],
    "shell": ["terminal"],
    "bash": ["terminal"],
    "web_search": ["web"],
    "web search": ["web"],
    "read_file": ["file"],
    "write_file": ["file"],
    "file": ["file"],
    "write_worker_result": ["terminal", "file"],
}


# Patterns that indicate tool hallucination even when no specific tool name is
# mentioned — the worker is claiming general tool/capability unavailability.
_CATCHALL_TOOL_HALLUCINATION_PATTERNS = [
    r"mandatory\s+(?:pre-)?completion\s+step.*(?:requires|not\s+available|cannot|unable)",
    r"pre-completion\s+step.*(?:requires|not\s+available|cannot|unable)",
    r"dispatched\s+without\s+(?:terminal|web|exec)\s+tools?",
    r"tools\s+return\s+(?:\"does\s+not\s+exist\"|does\s+not\s+exist)\s+errors?",
    r"return\s+\"?does\s+not\s+exist\"?\s+errors?",
    # Deferral with no tool named: worker says it WILL write the result later /
    # is waiting, instead of doing it now. (Genuine infra/uncertainty reasons
    # are already excluded by _GENUINE_BLOCK_SIGNALS at the top of the checker.)
    r"will\s+complete\s+(?:once|after|when).*(?:result\s+is\s+written|written\s+to\s+the\s+database)",
]


def _check_tool_hallucination(reason: str) -> Optional[str]:
    """If the block reason matches a tool-hallucination pattern, return an
    error message telling the worker to actually use its tools. Returns None
    if the reason doesn't look like a hallucination."""
    reason_lower = reason.lower()
    # GENUINE-BLOCK EXCLUSIONS (checked first): some reasons mention
    # write_worker_result but describe a REAL blocker, not a deferral/
    # hallucination — a genuine infra error or genuine uncertainty about how to
    # proceed. Forcing these workers to "just use your tools" is wrong and
    # unhelpful, so bail out before any pattern matching. Scoped tightly.
    _GENUINE_BLOCK_SIGNALS = (
        "database is locked", "database locked", "operationalerror",
        "disk i/o error", "no such file", "permission denied",
        "path and required arguments need verification",
        "arguments need verification", "don't have the exact",
        "do not have the exact", "not sure of the exact",
        "need to verify the", "exact script path",
    )
    if any(sig in reason_lower for sig in _GENUINE_BLOCK_SIGNALS):
        return None
    for pattern in _TOOL_HALLUCINATION_PATTERNS:
        if re.search(pattern, reason_lower):
            # Verify the tools ARE actually available
            try:
                from hermes_cli.tools_config import _get_platform_tools
                from hermes_cli.config import load_config
                config = load_config()
                tools = _get_platform_tools(config, "cli")
            except Exception:
                tools = set()  # If we can't check, don't block the block

            # Extract which tools the worker claims are missing
            claimed_missing = set()
            for keyword, tool_names in _TOOL_KEYWORD_MAP.items():
                if keyword in reason_lower:
                    claimed_missing.update(tool_names)

            # If claimed-missing tools are actually available, reject the block
            if claimed_missing and claimed_missing.issubset(tools):
                available = ", ".join(sorted(claimed_missing))
                return (
                    f"BLOCK REJECTED — tool hallucination detected. "
                    f"The worker claims it cannot use {available} but these "
                    f"tools ARE available in the current toolset. "
                    f"Use the terminal tool to run write_worker_result.py "
                    f"and then call kanban_complete. "
                    f"If you genuinely cannot run the script, describe the "
                    f"specific error you encountered, not a claim of missing tools."
                )
    # Catch-all: patterns that indicate tool hallucination without naming
    # specific tools (e.g. "mandatory completion step requires capabilities")
    for pattern in _CATCHALL_TOOL_HALLUCINATION_PATTERNS:
        if re.search(pattern, reason_lower):
            return (
                "BLOCK REJECTED — tool hallucination detected. "
                "The worker claims capabilities are unavailable without "
                "specifying which tool. All standard tools (terminal, web, "
                "file) are available. Use the terminal tool to run "
                "write_worker_result.py and then call kanban_complete."
            )
    return None


# ---------------------------------------------------------------------------
# Uncertainty-deferral guard
# ---------------------------------------------------------------------------

# Patterns that indicate the worker is asking for human review / guidance
# instead of just writing results and completing.
_UNCERTAINTY_DEFERRAL_PATTERNS = [
    r"human\s+review",
    r"requesting\s+(human\s+)?review",
    r"requesting\s+guidance",
    r"should\s+(i|we)\s+(call|run|use)\s+write_worker_result",
    r"should\s+(i|we)\s+mark\s+this",
    r"what\s+(should|i|we)\s+(do|write|submit)",
    r"direction\s+of\s+causation\s+is\s+debated",
    r"not\s+sure\s+how\s+to\s+(write|submit|record)",
    r"does\s+not\s+produce\s+a\s+finding",
    r"deliverable\s+is\s+a\s+resource\s+document",
    r"does\s+not\s+map\s+to\s+the\s+experiment\s+schema",
    r"research/literature\s+search",
    r"sub-delegation",
    r"not\s+an\s+experiment",
    r"cannot\s+determine\s+(the\s+)?(verdict|result|outcome)",
    r"need\s+human\s+input",
    r"blocked\s+for\s+review",
    r"awaiting\s+human",
]

_PROMETHEUS_DB = os.path.expanduser("~/.hermes/prometheus.db")


def _kanban_db_path() -> str:
    """Resolve the worker's pinned board DB the way the dispatcher does.

    The dispatcher injects ``HERMES_KANBAN_DB`` into every worker so the
    child sees exactly the board it was claimed from; fall back to the
    default board file otherwise.
    """
    return os.environ.get("HERMES_KANBAN_DB") or os.path.expanduser(
        "~/.hermes/kanban.db"
    )


def _check_uncertainty_deferral(tid: str, reason: str) -> Optional[str]:
    """Reject blocks where the worker asks for human review / guidance
    instead of just writing results and completing.

    Two cases:
    1. worker_results already exists → block rejected, tell worker to complete.
    2. Task body contains write_worker_result.py → block rejected, tell
       worker to run the command from the task body.
    """
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return None  # Only enforce for dispatcher-spawned workers

    reason_lower = reason.lower()
    matches_pattern = any(
        re.search(p, reason_lower) for p in _UNCERTAINTY_DEFERRAL_PATTERNS
    )
    if not matches_pattern:
        return None

    try:
        # Case 1: worker_results already exists → reject block, tell to complete
        if os.path.exists(_PROMETHEUS_DB):
            pconn = sqlite3.connect(
                f"file:{_PROMETHEUS_DB}?mode=ro", uri=True, timeout=5
            )
            row = pconn.execute(
                "SELECT 1 FROM worker_results WHERE kanban_task_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
            pconn.close()
            if row:
                return (
                    f"BLOCK REJECTED — worker_results already exist for task {tid}. "
                    f"Your results are already recorded. Call kanban_complete with "
                    f"status=completed immediately. Do NOT block for human review "
                    f"when results are already written."
                )

        # Case 2: task body contains write_worker_result.py → reject block,
        # tell worker to run the command from the task body
        db_path = _kanban_db_path()
        if not os.path.exists(db_path):
            return None
        kconn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        trow = kconn.execute(
            "SELECT body FROM tasks WHERE id = ? LIMIT 1", (tid,)
        ).fetchone()
        kconn.close()
        if not trow:
            return None
        body_raw = trow[0] or ""

        if "write_worker_result" in body_raw.lower():
            # Extract the write_worker_result.py command from the body
            cmd_match = re.search(
                r"(python3\s+~?/.*?write_worker_result\.py\s+\\?\n?(?:\s+--\S+.*?\n?)*)",
                body_raw,
                re.IGNORECASE,
            )
            cmd_hint = ""
            if cmd_match:
                cmd_hint = (
                    f"\nThe command from the task body is:\n{cmd_match.group(1).strip()}"
                )
            return (
                f"BLOCK REJECTED — this task's body already contains the "
                f"write_worker_result.py command. You MUST run it before completing. "
                f"Do NOT block asking for guidance — the instructions are in the "
                f"task body above. Just run the command and then call "
                f"kanban_complete.{cmd_hint}"
            )

    except Exception:
        logger.warning(
            "prometheus-guard: uncertainty-deferral check failed for task %s",
            tid,
            exc_info=True,
        )
        return None
    return None


# ---------------------------------------------------------------------------
# Worker-result-written completion gate (kanban_complete)
# ---------------------------------------------------------------------------
#
# Plugin port of the fork's ``_enforce_worker_result_written``
# (prometheus-fork tools/kanban_tools.py:470-560). Blocks completion of
# experiment tasks unless write_worker_result.py was called first, so
# findings are never silently lost — the experiments table is the ground
# truth for the dashboard and synthesis pipeline.
#
# Title classes gated (same as the fork):
#   * ``exp_`` prefix          → worker_results.experiment_id must match the
#                                 id extracted from the title
#   * ``[TRANSFER]`` in title  → worker_results.kanban_task_id must match
#   * ``[STRANDED`` in title   → worker_results.kanban_task_id must match
#
# EVERY failure path fails OPEN (returns None): missing env, missing DBs,
# locked DBs, schema drift, absent task rows. A guard bug must never strand
# a worker.


def _check_worker_result_written(tid: str) -> Optional[str]:
    """Return the fork's block message when an experiment-class task is
    being completed without a worker_results row; ``None`` to allow."""
    # Only enforce for dispatcher-spawned workers (not orchestrators / CLI).
    if not os.environ.get("HERMES_KANBAN_TASK"):
        logger.debug("gate: no HERMES_KANBAN_TASK env, skipping")
        return None

    try:
        db_path = _kanban_db_path()
        if not os.path.exists(db_path):
            return None  # Board DB missing — don't block
        kconn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        trow = kconn.execute(
            "SELECT title, body FROM tasks WHERE id = ? LIMIT 1", (tid,)
        ).fetchone()
        kconn.close()

        if not trow or not trow[0]:
            logger.debug("gate: task %s not found or no title, skipping", tid)
            return None

        title = str(trow[0]).strip()
        # Only gate experiment tasks — synthesis / curation / other pass through.
        if not (
            title.lower().startswith("exp_")
            or "[transfer]" in title.lower()
            or "[stranded" in title.lower()
        ):
            logger.debug(
                "gate: title '%s' doesn't match gate pattern, skipping", title[:60]
            )
            return None

        # Extract experiment ID from title (e.g. "exp_8501: ..." → "exp_8501")
        exp_id = None
        if title.lower().startswith("exp_"):
            exp_id = title.split(":")[0].split()[0].strip()
        else:
            # For [TRANSFER] and [STRANDED] tasks, extract from body
            body_text = str(trow[1] or "")
            match = re.search(r"exp_(\d+)", body_text)
            if match:
                exp_id = f"exp_{match.group(1)}"

        # Query prometheus.db for a worker_results row.
        # For [TRANSFER]/[STRANDED] tasks: check kanban_task_id FIRST (authoritative).
        # For exp_ tasks: check experiment_id (from title).
        if not os.path.exists(_PROMETHEUS_DB):
            return None  # DB missing — don't block

        pconn = sqlite3.connect(
            f"file:{_PROMETHEUS_DB}?mode=ro", uri=True, timeout=5
        )
        row = None
        # For [TRANSFER]/[STRANDED] tasks: check by kanban_task_id (the actual
        # task ID). Do NOT check by experiment_id — the body contains the
        # SOURCE experiment ID, not this task's ID, so an experiment_id lookup
        # would match the wrong result.
        if "[transfer]" in title.lower() or "[stranded" in title.lower():
            row = pconn.execute(
                "SELECT 1 FROM worker_results WHERE kanban_task_id = ? LIMIT 1",
                (tid,),
            ).fetchone()
            logger.debug(
                "gate: [TRANSFER] task %s, kanban_task_id lookup=%s",
                tid,
                "FOUND" if row else "NOT FOUND",
            )
        elif exp_id:
            # For exp_ prefixed tasks: check by experiment_id (from title).
            row = pconn.execute(
                "SELECT 1 FROM worker_results WHERE experiment_id = ? LIMIT 1",
                (exp_id,),
            ).fetchone()
            logger.debug(
                "gate: exp_ task %s, experiment_id=%s lookup=%s",
                tid,
                exp_id,
                "FOUND" if row else "NOT FOUND",
            )
        pconn.close()

        if row:
            return None  # Result exists — allow completion

        logger.warning(
            "gate: BLOCKED task %s (title='%s') — no worker_results found",
            tid,
            title[:60],
        )
        return (
            f"kanban_complete blocked: no worker_results entry found for task {tid}. "
            f"You MUST call write_worker_result.py BEFORE kanban_complete. "
            f"Without it, your findings never reach the dashboard or synthesis. "
            f"Example:\n"
            f"python3 ~/.hermes/scripts/write_worker_result.py "
            f"--experiment <id> --finding \"YOUR KEY FINDING\" "
            f"--confidence 0.8 --domain <domain> --tags CONFIRMED"
        )
    except Exception:
        logger.warning("gate: EXCEPTION checking task %s", tid, exc_info=True)
        return None


def _handle_kanban_complete(
    args: Dict[str, Any], task_id: str
) -> Optional[Dict[str, str]]:
    """pre_tool_call branch for ``kanban_complete`` — worker-result gate.

    Resolves the task id the same way the fork's ``_default_task_id`` does
    (explicit arg → HERMES_KANBAN_TASK env) and fails OPEN on any doubt.
    """
    try:
        tid = str(
            args.get("task_id")
            or task_id
            or os.environ.get("HERMES_KANBAN_TASK")
            or ""
        ).strip()
        if not tid:
            # Let kanban_complete's own "task_id is required" error fire.
            return None
        msg = _check_worker_result_written(tid)
        if msg:
            logger.info(
                "prometheus-guard: blocked kanban_complete (task=%s): no "
                "worker_results row",
                tid,
            )
            return {"action": "block", "message": msg}
        return None
    except Exception:
        logger.warning(
            "prometheus-guard: completion gate failed open", exc_info=True
        )
        return None


# ---------------------------------------------------------------------------
# Hook wiring
# ---------------------------------------------------------------------------


def _pre_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    task_id: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Fires for every tool call; engages on ``kanban_block`` (bogus-block
    guards) and ``kanban_complete`` (worker-result-written gate).

    Returning ``{"action": "block", "message": ...}`` makes the tool call
    return ``{"error": message}`` to the model without executing — the
    same result shape the in-file guard produced via ``tool_error()``.
    """
    if tool_name == "kanban_complete":
        return _handle_kanban_complete(
            args if isinstance(args, dict) else {}, task_id
        )
    if tool_name != "kanban_block":
        return None
    args = args if isinstance(args, dict) else {}
    reason = str(args.get("reason") or "")
    if not reason.strip():
        # Let kanban_block's own "reason is required" error fire.
        return None

    msg = _check_tool_hallucination(reason)
    if msg is None:
        tid = str(
            args.get("task_id")
            or task_id
            or os.environ.get("HERMES_KANBAN_TASK")
            or ""
        )
        if tid:
            msg = _check_uncertainty_deferral(tid, reason)

    if msg:
        logger.info(
            "prometheus-guard: rejected kanban_block (task=%s): %.120s",
            args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK") or "?",
            reason,
        )
        return {"action": "block", "message": msg}
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    logger.debug("prometheus-guard: pre_tool_call guard registered")
