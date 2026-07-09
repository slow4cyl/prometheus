# Prometheus Disabled-Feature Audit (2026-07-01)

## What was disabled

| Feature | Location | Status | Notes |
|---|---|---|---|
| gpu_sklearn_hook.pth | hermes-agent venv site-packages | Re-enabled (was .disabled) | Recurring: changelog line ~363 documents same fix on 2026-06-28 |
| gpu_sklearn_hook.pth | vllm-env site-packages | Already active | Never disabled |
| self_mod (self-modification) | architecture-map line ~60 | Intentionally disabled | Gated by model quality; architecturally intact |
| browser/browser_use plugin | config.yaml plugins.disabled | Intentionally disabled | |
| memory.user_profile_enabled | config.yaml | Intentionally disabled (false) | |
| model_catalog.enabled | config.yaml | Intentionally disabled (false) | |
| tools.tool_search.enabled | config.yaml | Intentionally disabled (false) | |
| human_delay.mode | config.yaml | Intentionally disabled ('off') | |
| security.website_blocklist | config.yaml | Intentionally disabled (false) | |

## What was NOT disabled (verified clean)

- No masked systemd services
- No failed systemd services (except xdg-desktop-portal-gtk, unrelated)
- No paused cron jobs
- No other disabled .pth hooks
- No disabled cron entries in crontab

## Key finding

The gpu_sklearn hook disabling is a RECURRING issue. The changelog documents it being fixed on 202-06-28, but it was disabled again by July 1. The root cause of the repeated disabling is unknown — possibly a `hermes update` or venv reinstallation that wipes/restores site-packages. Future investigations should check if a hermes update occurred between the re-enable date and the next occurrence.
