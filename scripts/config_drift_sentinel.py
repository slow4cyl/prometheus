#!/usr/bin/env python3
"""
Config-drift sentinel for the live Hermes config.yaml.

Standing guard for the pinned free-worker model lane: a test suite once
clobbered model.default/model.provider to an Anthropic endpoint, which
silently killed the whole worker fleet (paid endpoint + wrong protocol).
This sentinel catches that failure class within one cron tick.

Checks (against <HERMES_HOME>/config.yaml, parsed with PyYAML):
  1. model_default_drift   — model.default != the pinned worker model
                             (default "xiaomi/mimo-v2.5", override via
                             env CONFIG_DRIFT_EXPECT_MODEL).
  2. model_provider_drift  — model.provider != the pinned provider
                             (default "openrouter", override via
                             env CONFIG_DRIFT_EXPECT_PROVIDER).
  3. missing_plugin        — plugins.enabled no longer contains one of
                             the required Prometheus plugins:
                             prometheus-guard, prometheus-prompt-policy,
                             prometheus-runtime-tuning. One alert per
                             missing plugin.

Fail-open contract: if config.yaml is missing, unreadable, or unparseable
(or PyYAML itself is unavailable), the sentinel exits 0 silently — it can
only assert on a config it can read, and it must never crash the cron lane.
A readable-but-wrong config (missing keys, empty file) IS drift and alerts.

Conventions (match sibling monitors): silent + exit 0 when healthy; on
alert, print one "ALERT ..." line per problem to stdout and exit 1. Always
writes <HERMES_HOME>/config_drift_report.json (best-effort).
"""
import json
import os
import sys
import time

try:
    from prometheus_paths import HERMES_HOME
except ImportError:
    HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))

CONFIG_PATH = os.path.join(HERMES_HOME, "config.yaml")
REPORT_PATH = os.path.join(HERMES_HOME, "config_drift_report.json")

EXPECTED_MODEL = os.environ.get("CONFIG_DRIFT_EXPECT_MODEL", "xiaomi/mimo-v2.5")
EXPECTED_PROVIDER = os.environ.get("CONFIG_DRIFT_EXPECT_PROVIDER", "openrouter")
REQUIRED_PLUGINS = (
    "prometheus-guard",
    "prometheus-prompt-policy",
    "prometheus-runtime-tuning",
)


def write_report(payload):
    """Best-effort report write; never lets reporting break the exit contract."""
    try:
        with open(REPORT_PATH, "w") as f:
            json.dump(payload, f, indent=1, default=str)
    except OSError:
        pass


def load_config():
    """Return (config_dict, skip_reason). skip_reason is set when the sentinel
    must fail open (config missing/unreadable/unparseable, or no PyYAML)."""
    try:
        import yaml
    except ImportError as e:
        return None, f"pyyaml_unavailable: {e}"
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f), None
    except OSError as e:
        return None, f"config_unreadable: {e}"
    except yaml.YAMLError as e:
        return None, f"config_unparseable: {e}"


def dig(cfg, *keys):
    """Nested dict lookup; returns None when any level is absent/mistyped."""
    cur = cfg
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def main():
    now = time.time()
    cfg, skip_reason = load_config()

    if skip_reason is not None:
        # Fail OPEN: cannot read the config, so there is nothing to assert.
        write_report({
            "checked_at": now,
            "healthy": True,
            "skipped": skip_reason,
            "config_path": CONFIG_PATH,
            "alerts": [],
        })
        sys.exit(0)

    alerts = []

    model_default = dig(cfg, "model", "default")
    model_provider = dig(cfg, "model", "provider")
    enabled = dig(cfg, "plugins", "enabled")
    enabled_list = enabled if isinstance(enabled, list) else []

    if model_default != EXPECTED_MODEL:
        alerts.append(
            f"model_default_drift: config.yaml model.default is {model_default!r} "
            f"(expected {EXPECTED_MODEL!r}); the pinned free worker lane was clobbered")
    if model_provider != EXPECTED_PROVIDER:
        alerts.append(
            f"model_provider_drift: config.yaml model.provider is {model_provider!r} "
            f"(expected {EXPECTED_PROVIDER!r}); the pinned free worker lane was clobbered")
    for plugin in REQUIRED_PLUGINS:
        if plugin not in enabled_list:
            alerts.append(
                f"missing_plugin: plugins.enabled no longer contains {plugin!r} "
                f"(enabled={enabled_list!r})")

    write_report({
        "checked_at": now,
        "healthy": not alerts,
        "config_path": CONFIG_PATH,
        "expected": {
            "model.default": EXPECTED_MODEL,
            "model.provider": EXPECTED_PROVIDER,
            "required_plugins": list(REQUIRED_PLUGINS),
        },
        "observed": {
            "model.default": model_default,
            "model.provider": model_provider,
            "plugins.enabled": enabled if isinstance(enabled, list) else enabled,
        },
        "alerts": alerts,
    })

    if alerts:
        for a in alerts:
            print(f"ALERT {a}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
