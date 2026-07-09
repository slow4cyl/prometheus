"""prometheus-runtime-tuning — two independent sentinel-guarded runtime patches.

Replaces two fork hunks per ~/.hermes/docs/defork-plan.md (both rows route
``runtime_patch_plugin``, status CONFIRMED):

1. **cron_min_grace** — upstream ``cron/jobs.py`` hardcodes ``MIN_GRACE = 120``
   inside ``_compute_grace_seconds`` (origin/main :584); the fork raised it to
   300 for missed-run fast-forward on this loaded box. Here we WRAP, not
   replace: rebind ``cron.jobs._compute_grace_seconds`` to
   ``max(300, orig(...))``. Works because the sole call site
   (``_get_due_jobs_locked``, cron/jobs.py:1601 fork / :1777 origin/main) does
   a late-bound module-global lookup, and the gateway loads user plugins
   (gateway/run.py:6696-6697, inside ``GatewayRunner.start``) BEFORE starting
   the cron-scheduler thread (gateway/run.py:20077-20087).

2. **kanban_redaction** — upstream ``tools/kanban_tools.py`` redacts
   ``kanban_complete`` summary/result/meta (origin/main :518/:520/:523) and
   ``kanban_comment`` body (:808) at DB-write time with ``force=True``, which
   destroys values the prometheus fleet needs verbatim. No knob/hook reaches a
   force=True write, so rebind the module-level name
   ``tools.kanban_tools.redact_sensitive_text`` to identity.
   KNOWN BEHAVIORAL DELTA (accepted per plan, logged at patch time): the fork
   RETAINED block-reason redaction (fork tools/kanban_tools.py:1067); this
   module-level rebind removes that too.

Guarantees, per patch, independently:
  - sentinel guard: the upstream symbol's current source must contain an exact
    expected substring of the origin/main base text; on mismatch we do NOT
    patch, write ``$HERMES_HOME/PATCH_FAILED_<name>`` with details, and log an
    ERROR — base behavior stays intact (fail open, never raise into the host).
  - fork-shape no-op: if the module is already fork-shaped (pre-surgery live
    tree: ``MIN_GRACE = 300`` / summary-redact call absent while the retained
    block-reason call is present), skip silently — no patch, no marker.
  - idempotent: re-running plugin discovery never double-wraps.
"""

from __future__ import annotations

import inspect
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN = "prometheus-runtime-tuning"


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _write_marker(name: str, details: str) -> None:
    """Record a failed/refused patch. Never raises."""
    try:
        marker = _hermes_home() / f"PATCH_FAILED_{name}"
        marker.write_text(
            f"plugin: {_PLUGIN}\n"
            f"patch: {name}\n"
            f"time: {datetime.now(timezone.utc).isoformat()}\n"
            f"details: {details}\n"
            "action: base behavior left INTACT (fail open). Re-verify the "
            "upstream symbol against this plugin's sentinel fingerprints and "
            "update the plugin, then delete this marker.\n"
        )
    except Exception:
        logger.exception("%s: could not write PATCH_FAILED_%s marker", _PLUGIN, name)
    logger.error("%s: PATCH FAILED (%s): %s — base behavior left intact", _PLUGIN, name, details)


# ---------------------------------------------------------------------------
# Patch 1 — cron MIN_GRACE floor 120 -> 300
# ---------------------------------------------------------------------------

def _patch_cron_min_grace() -> None:
    name = "cron_min_grace"
    import cron.jobs as _cj

    fn = getattr(_cj, "_compute_grace_seconds", None)
    if fn is None:
        _write_marker(name, "cron.jobs._compute_grace_seconds does not exist (upstream refactor?)")
        return
    if getattr(fn, "_prometheus_min_grace_wrapper", False):
        logger.debug("%s: %s already wrapped — no-op", _PLUGIN, name)
        return

    try:
        src = inspect.getsource(fn)
    except Exception as exc:
        _write_marker(name, f"could not read source of _compute_grace_seconds: {exc!r}")
        return

    # Fork-shaped base (pre-surgery live tree): fork hunk already sets 300.
    if "MIN_GRACE = 300" in src:
        logger.info("%s: %s — base is already fork-shaped (MIN_GRACE = 300); no-op", _PLUGIN, name)
        return

    # Sentinel: exact substrings of the origin/main base text (cron/jobs.py:577,:584).
    if "def _compute_grace_seconds(schedule" not in src or "MIN_GRACE = 120" not in src:
        _write_marker(
            name,
            "sentinel mismatch: expected origin/main fingerprints "
            "'def _compute_grace_seconds(schedule' and 'MIN_GRACE = 120' in the "
            f"function source; got:\n{src[:400]}",
        )
        return

    _orig = fn

    def _compute_grace_seconds_min300(*args, **kwargs):
        return max(300, _orig(*args, **kwargs))

    _compute_grace_seconds_min300._prometheus_min_grace_wrapper = True
    _compute_grace_seconds_min300.__wrapped__ = _orig
    _compute_grace_seconds_min300.__name__ = getattr(_orig, "__name__", "_compute_grace_seconds")
    _compute_grace_seconds_min300.__doc__ = (
        f"[{_PLUGIN}] wrapped: max(300, orig(...)). " + (getattr(_orig, "__doc__", None) or "")
    )
    _cj._compute_grace_seconds = _compute_grace_seconds_min300
    logger.info(
        "%s: %s — wrapped cron.jobs._compute_grace_seconds, MIN_GRACE floor 120 -> 300",
        _PLUGIN, name,
    )


# ---------------------------------------------------------------------------
# Patch 2 — remove redaction from kanban_complete outputs / kanban_comment body
# ---------------------------------------------------------------------------

# Exact substrings of the origin/main base text (tools/kanban_tools.py).
_UPSTREAM_SUMMARY_CALL = "summary = redact_sensitive_text(str(summary), force=True)"   # :518
_UPSTREAM_BODY_CALL = "body = redact_sensitive_text(str(body), force=True)"            # :808
_RETAINED_REASON_CALL = "reason = redact_sensitive_text(str(reason), force=True)"      # fork :1067 / upstream :681


def _patch_kanban_redaction() -> None:
    name = "kanban_redaction"
    import tools.kanban_tools as _kt

    cur = getattr(_kt, "redact_sensitive_text", None)
    if cur is None:
        _write_marker(name, "tools.kanban_tools.redact_sensitive_text does not exist (upstream refactor?)")
        return
    if getattr(cur, "_prometheus_identity_rebind", False):
        logger.debug("%s: %s already rebound — no-op", _PLUGIN, name)
        return

    try:
        src = inspect.getsource(_kt)
    except Exception as exc:
        _write_marker(name, f"could not read source of tools.kanban_tools: {exc!r}")
        return

    if _UPSTREAM_SUMMARY_CALL not in src or _UPSTREAM_BODY_CALL not in src:
        # Fork-shaped base (pre-surgery live tree): complete/comment redaction
        # already removed; only the retained block-reason call remains. Rebinding
        # here would strip block-reason redaction the fork deliberately kept, so
        # no-op instead.
        if _RETAINED_REASON_CALL in src:
            logger.info(
                "%s: %s — base is already fork-shaped (complete/comment redaction "
                "absent, block-reason call retained); no-op", _PLUGIN, name,
            )
            return
        _write_marker(
            name,
            "sentinel mismatch: neither the origin/main fingerprints "
            f"({_UPSTREAM_SUMMARY_CALL!r} + {_UPSTREAM_BODY_CALL!r}) nor the "
            f"fork-shape fingerprint ({_RETAINED_REASON_CALL!r}) found in "
            "tools/kanban_tools.py source (upstream refactor?)",
        )
        return

    # Upstream-shaped. Confirm the binding is still the canonical function —
    # if some other code already rebound it, refuse rather than stack patches.
    try:
        from agent.redact import redact_sensitive_text as _canonical
    except Exception as exc:
        _write_marker(name, f"could not import agent.redact.redact_sensitive_text for identity check: {exc!r}")
        return
    if cur is not _canonical:
        _write_marker(
            name,
            "tools.kanban_tools.redact_sensitive_text is no longer "
            "agent.redact.redact_sensitive_text — already rebound by other code; refusing to stack",
        )
        return

    def _identity_redact(text, **kwargs):  # matches (text, *, force=..., ...) call shapes
        return text

    _identity_redact._prometheus_identity_rebind = True
    _kt.redact_sensitive_text = _identity_redact
    logger.info(
        "%s: %s — rebound tools.kanban_tools.redact_sensitive_text to identity: "
        "kanban_complete summary/result/meta and kanban_comment body are no longer "
        "redacted at write time. BEHAVIORAL DELTA vs fork: the fork retained "
        "block-reason redaction (fork tools/kanban_tools.py:1067); this module-level "
        "rebind removes it too (accepted per defork-plan).",
        _PLUGIN, name,
    )


# ---------------------------------------------------------------------------
# Apply at import time — each patch independent, fail open.
# ---------------------------------------------------------------------------

for _apply, _name in (
    (_patch_cron_min_grace, "cron_min_grace"),
    (_patch_kanban_redaction, "kanban_redaction"),
):
    try:
        _apply()
    except Exception:
        try:
            _write_marker(_name, "unexpected exception during patch:\n" + traceback.format_exc())
        except Exception:
            pass  # never let a patch failure propagate into the host process


def register(ctx):
    """No tools/hooks/middleware — both patches run at module import above."""
    return None
