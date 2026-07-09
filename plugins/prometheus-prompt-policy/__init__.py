"""prometheus-prompt-policy — runtime patch of the memory-policy prompts.

De-fork runtime_patch_plugin for the defork-plan rows:
  * "prompts — MEMORY_GUIDANCE rewrite: system-facts-only memory policy"
  * "prompts — _MEMORY_REVIEW_PROMPT rewrite: background memory review
    scoped to system facts"

What it does (at plugin load, i.e. inside ``discover_plugins()`` which runs
in every agent process before the first system-prompt build):

1. ``MEMORY_GUIDANCE`` — replaces the upstream constant with the fork text
   (system-facts-only memory policy) on BOTH live bindings:
   ``agent.prompt_builder.MEMORY_GUIDANCE`` (source of truth) and
   ``agent.system_prompt.MEMORY_GUIDANCE`` (top-level from-import binding,
   the one ``build_system_prompt`` actually reads).
2. ``_MEMORY_REVIEW_PROMPT`` — replaces the upstream text with the fork text
   on the ``agent.background_review._MEMORY_REVIEW_PROMPT`` module global
   and on ``run_agent.AIAgent._MEMORY_REVIEW_PROMPT`` (the operative class
   attribute — ``background_review`` selects via
   ``getattr(agent, name, module_default)`` so instance→class MRO wins), so
   one surviving seam suffices.

   run_agent is NEVER imported eagerly by this plugin: run_agent.py:136
   imports model_tools, whose module top (model_tools.py:206) calls
   ``discover_plugins()`` — i.e. this plugin can load while ``run_agent`` is
   only partially initialized (before ``class AIAgent`` at run_agent.py:403).
   Instead the module globals are patched FIRST; the class body's
   ``from agent.background_review import ...`` (run_agent.py:1551-1555) then
   binds the already-patched values at class creation. The class attributes
   are patched directly only when ``sys.modules['run_agent']`` already holds
   a fully-created ``AIAgent`` (covers processes where run_agent was imported
   before plugin discovery). A partially-initialized run_agent is therefore
   NOT a failure.
3. ``_COMBINED_REVIEW_PROMPT`` — splices the same memory-scope guardrail
   into the combined memory+skill review prompt (the fork itself MISSED this
   path; upstream and fork texts are identical, so this splice is active on
   both pre- and post-defork trees). Patched on the AIAgent class attribute
   and the ``agent.background_review`` module global, like (2).

Sentinel guard (fail open, never crash the host):
  For each symbol —
  * current value == fork/target text  -> no-op (pre-surgery live tree, or
    plugin already ran in this process). NOT a failure.
  * current value == the exact upstream text, or contains the distinctive
    upstream fingerprint substring          -> patch.
  * anything else (upstream drifted/reworded) -> do NOT patch; leave base
    behavior intact, log loudly, and write
    ``$HERMES_HOME/PATCH_FAILED_prometheus-prompt-policy`` with details.
  A pre-existing marker file is never deleted by a later clean run — it is
  an operator tripwire; remove it by hand after investigating.

All imports of agent modules are guarded: any import failure is recorded in
the marker file and the plugin degrades to a no-op (fail open).
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_NAME = "prometheus-prompt-policy"


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


# ---------------------------------------------------------------------------
# Texts — extracted byte-exact (ast.literal_eval) from git:
#   fork targets:   prometheus-fork:agent/prompt_builder.py,
#                   prometheus-fork:agent/background_review.py
#   upstream bases: origin/main:agent/prompt_builder.py,
#                   origin/main:agent/background_review.py  (@ 2026-07-08)
# ---------------------------------------------------------------------------


# --- 1. MEMORY_GUIDANCE -----------------------------------------------------

FORK_MEMORY_GUIDANCE = "You have persistent memory across sessions. Save durable facts using the memory tool: environment details, tool quirks, stable conventions, and system infrastructure. Memory is injected into every turn, so keep it compact and focused on facts that will still matter later.\nDO NOT save user preferences, behavioral patterns, communication style, or anything about the user's personality. That information is not yours to capture — it bleeds into worker sessions and corrupts their behavior. Only save factual system knowledge.\nDo NOT save task progress, session outcomes, completed-work logs, or temporary TODO state to memory; use session_search to recall those from past transcripts. Specifically: do not record PR numbers, issue numbers, commit SHAs, 'fixed bug X', 'submitted PR Y', 'Phase N done', file counts, or any artifact that will be stale in 7 days. If a fact will be stale in a week, it does not belong in memory. If you've discovered a new way to do something, solved a problem that could be necessary later, save it as a skill with the skill tool.\nWrite memories as declarative facts, not instructions to yourself. 'Project uses pytest with xdist' ✓ — 'Run tests with pytest -n 4' ✗. Imperative phrasing gets re-read as a directive in later sessions and can cause repeated work or override the user's current request. Procedures and workflows belong in skills, not memory."

UPSTREAM_MEMORY_GUIDANCE = "You have persistent memory across sessions. Save durable facts using the memory tool: user preferences, environment details, tool quirks, and stable conventions. Memory is injected into every turn, so keep it compact and focused on facts that will still matter later.\nPrioritize what reduces future user steering — the most valuable memory is one that prevents the user from having to correct or remind you again. User preferences and recurring corrections matter more than procedural task details.\nDo NOT save task progress, session outcomes, completed-work logs, or temporary TODO state to memory; use session_search to recall those from past transcripts. Specifically: do not record PR numbers, issue numbers, commit SHAs, 'fixed bug X', 'submitted PR Y', 'Phase N done', file counts, or any artifact that will be stale in 7 days. If a fact will be stale in a week, it does not belong in memory. If you've discovered a new way to do something, solved a problem that could be necessary later, save it as a skill with the skill tool.\nWrite memories as declarative facts, not instructions to yourself. 'User prefers concise responses' ✓ — 'Always respond concisely' ✗. 'Project uses pytest with xdist' ✓ — 'Run tests with pytest -n 4' ✗. Imperative phrasing gets re-read as a directive in later sessions and can cause repeated work or override the user's current request. Procedures and workflows belong in skills, not memory."

# Distinctive substring of the UPSTREAM text, absent from the fork text.
UPSTREAM_MG_FINGERPRINT = (
    "user preferences, environment details, tool quirks, and stable conventions"
)

# --- 2. _MEMORY_REVIEW_PROMPT ------------------------------------------------

FORK_MEMORY_REVIEW_PROMPT = "Review the conversation above and consider saving to memory if appropriate.\n\nFocus on:\n1. Has a new system infrastructure fact emerged — new tool, new script, new config?\n2. Has a debugging pattern or workaround been discovered that future sessions need?\n\nDO NOT save user preferences, behavioral patterns, communication style, persona, desires, or anything about the user's personality. Only save factual system knowledge.\n\nIf something stands out, save it using the memory tool. If nothing is worth saving, just say 'Nothing to save.' and stop."

UPSTREAM_MEMORY_REVIEW_PROMPT = "Review the conversation above and consider saving to memory if appropriate.\n\nFocus on:\n1. Has the user revealed things about themselves — their persona, desires, preferences, or personal details worth remembering?\n2. Has the user expressed expectations about how you should behave, their work style, or ways they want you to operate?\n\nIf something stands out, save it using the memory tool. If nothing is worth saving, just say 'Nothing to save.' and stop."

UPSTREAM_MRP_FINGERPRINT = "Has the user revealed things about themselves"

# --- 3. _COMBINED_REVIEW_PROMPT splice ----------------------------------------
# Upstream == fork here (the fork missed this path). Two guardrail splices:
# the **Memory** section and the "User-preference embedding" passage that
# directs preference data into memory.

COMBINED_MEMORY_OLD = '**Memory**: who the user is. Did the user reveal persona, desires, preferences, personal details, or expectations about how you should behave? Save facts about the user and durable preferences with the memory tool.\n\n'

COMBINED_MEMORY_NEW = (
    "**Memory**: system facts only. Has a new system infrastructure fact "
    "emerged \u2014 new tool, new script, new config? Has a debugging pattern or "
    "workaround been discovered that future sessions need? Save those with "
    "the memory tool. DO NOT save user preferences, behavioral patterns, "
    "communication style, persona, desires, or anything about the user's "
    "personality \u2014 that bleeds into worker sessions and corrupts their "
    "behavior. Only save factual system knowledge.\n\n"
)

COMBINED_PREF_OLD = "User-preference embedding: when the user complains about how you handled a task, update the skill that governs that task — memory alone isn't enough. Memory says 'who the user is and what the current situation and state of your operations are'; skills say 'how to do this class of task for this user'. Both should carry user-preference lessons when relevant.\n\n"

COMBINED_PREF_NEW = (
    "User-preference embedding: when the user complains about how you "
    "handled a task, update the skill that governs that task \u2014 that is "
    "where the lesson belongs. Skills say 'how to do this class of task for "
    "this user'; memory is reserved for factual system knowledge and must "
    "NOT carry user-preference or personality data.\n\n"
)

# Idempotence marker: present iff the guardrail splice has been applied.
COMBINED_GUARD_MARKER = "Only save factual system knowledge.\n\n"


# ---------------------------------------------------------------------------
# Patch machinery
# ---------------------------------------------------------------------------

def _decide_replace(cur, fork_text, upstream_full, upstream_fingerprint):
    """Return ("noop"|"patch"|"fail", new_value_or_detail)."""
    if cur == fork_text:
        return ("noop", None)
    if not isinstance(cur, str):
        return ("fail", "symbol missing or not a str: %r" % (type(cur).__name__,))
    if cur == upstream_full or upstream_fingerprint in cur:
        return ("patch", fork_text)
    return ("fail",
            "text matches neither fork target nor upstream fingerprint "
            "(upstream drift?); head=%r" % (cur[:120],))


def _decide_splice_combined(cur):
    if not isinstance(cur, str):
        return ("fail", "symbol missing or not a str: %r" % (type(cur).__name__,))
    if COMBINED_GUARD_MARKER in cur:
        return ("noop", None)
    if COMBINED_MEMORY_OLD in cur and COMBINED_PREF_OLD in cur:
        new = cur.replace(COMBINED_MEMORY_OLD, COMBINED_MEMORY_NEW, 1)
        new = new.replace(COMBINED_PREF_OLD, COMBINED_PREF_NEW, 1)
        return ("patch", new)
    return ("fail",
            "combined prompt lacks the expected memory-section / "
            "user-preference-embedding base substrings (upstream drift?); "
            "head=%r" % (cur[:120],))


def _write_marker(problems):
    """Write $HERMES_HOME/PATCH_FAILED_<name>. Never raises."""
    lines = [
        "plugin: %s" % PLUGIN_NAME,
        "time: %s" % datetime.datetime.now().isoformat(),
        "the runtime prompt patches were NOT (fully) applied; base behavior",
        "left intact (fail open). Details:",
        "",
    ]
    lines += ["- %s: %s" % (target, detail) for target, detail in problems]
    body = "\n".join(lines) + "\n"
    try:
        marker = _hermes_home() / ("PATCH_FAILED_" + PLUGIN_NAME)
        marker.write_text(body, encoding="utf-8")
    except Exception:
        logger.error("%s: could not write PATCH_FAILED marker", PLUGIN_NAME,
                      exc_info=True)
    try:
        sys.stderr.write("[%s] PATCH FAILED (fail-open, base prompts kept):\n%s"
                          % (PLUGIN_NAME, body))
    except Exception:
        pass
    logger.error("[%s] PATCH FAILED (fail-open): %s", PLUGIN_NAME, problems)


def _apply():
    """Apply all prompt patches. Idempotent; never raises."""
    problems = []   # (target, detail)
    applied = []    # (target, status)

    # -- guarded imports ----------------------------------------------------
    _pb = _sp = _br = _agent_cls = None
    try:
        from agent import prompt_builder as _pb  # noqa: F811
    except Exception as exc:
        problems.append(("import agent.prompt_builder", repr(exc)))
    try:
        from agent import system_prompt as _sp  # noqa: F811
    except Exception as exc:
        problems.append(("import agent.system_prompt", repr(exc)))
    try:
        from agent import background_review as _br  # noqa: F811
    except Exception as exc:
        problems.append(("import agent.background_review", repr(exc)))
    # run_agent is deliberately NOT imported here (see module docstring:
    # discover_plugins() can fire mid-run_agent-import via model_tools).
    # If it is not (fully) imported yet, the AIAgent class body will bind
    # the module globals we patch below — skipping is correct, not a failure.
    try:
        _ra = sys.modules.get("run_agent")
        if _ra is not None:
            _agent_cls = getattr(_ra, "AIAgent", None)
    except Exception as exc:
        problems.append(("run_agent.AIAgent lookup", repr(exc)))

    # -- 1. MEMORY_GUIDANCE on both live bindings ----------------------------
    for owner, desc in ((_pb, "agent.prompt_builder.MEMORY_GUIDANCE"),
                        (_sp, "agent.system_prompt.MEMORY_GUIDANCE")):
        if owner is None:
            continue
        verdict, payload = _decide_replace(
            getattr(owner, "MEMORY_GUIDANCE", None),
            FORK_MEMORY_GUIDANCE,
            UPSTREAM_MEMORY_GUIDANCE,
            UPSTREAM_MG_FINGERPRINT,
        )
        if verdict == "patch":
            owner.MEMORY_GUIDANCE = payload
            applied.append((desc, "patched"))
        elif verdict == "noop":
            applied.append((desc, "already fork text (no-op)"))
        else:
            problems.append((desc, payload))

    # -- 2. _MEMORY_REVIEW_PROMPT: AIAgent class attr + module-global fallback
    for owner, desc in ((_agent_cls, "run_agent.AIAgent._MEMORY_REVIEW_PROMPT"),
                        (_br, "agent.background_review._MEMORY_REVIEW_PROMPT")):
        if owner is None:
            continue
        verdict, payload = _decide_replace(
            getattr(owner, "_MEMORY_REVIEW_PROMPT", None),
            FORK_MEMORY_REVIEW_PROMPT,
            UPSTREAM_MEMORY_REVIEW_PROMPT,
            UPSTREAM_MRP_FINGERPRINT,
        )
        if verdict == "patch":
            setattr(owner, "_MEMORY_REVIEW_PROMPT", payload)
            applied.append((desc, "patched"))
        elif verdict == "noop":
            applied.append((desc, "already fork text (no-op)"))
        else:
            problems.append((desc, payload))

    # -- 3. _COMBINED_REVIEW_PROMPT guardrail splice --------------------------
    for owner, desc in ((_agent_cls, "run_agent.AIAgent._COMBINED_REVIEW_PROMPT"),
                        (_br, "agent.background_review._COMBINED_REVIEW_PROMPT")):
        if owner is None:
            continue
        verdict, payload = _decide_splice_combined(
            getattr(owner, "_COMBINED_REVIEW_PROMPT", None))
        if verdict == "patch":
            setattr(owner, "_COMBINED_REVIEW_PROMPT", payload)
            applied.append((desc, "guardrail spliced"))
        elif verdict == "noop":
            applied.append((desc, "guardrail already present (no-op)"))
        else:
            problems.append((desc, payload))

    if problems:
        _write_marker(problems)
    if applied:
        logger.info("[%s] prompt policy applied: %s", PLUGIN_NAME,
                     "; ".join("%s -> %s" % a for a in applied))
    return applied, problems


def register(ctx):
    """Plugin entry point. No hooks/tools — re-runs the (idempotent) patch."""
    try:
        _apply()
    except Exception:
        logger.error("[%s] register() failed (fail-open)", PLUGIN_NAME,
                      exc_info=True)


# Import-time application: discover_plugins() imports this module before the
# first system-prompt build, so the patch is live even if register() were
# never called.
try:
    _apply()
except Exception:  # pragma: no cover — absolute fail-open backstop
    logger.error("[%s] import-time apply failed (fail-open)", PLUGIN_NAME,
                  exc_info=True)
