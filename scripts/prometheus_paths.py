"""Single source of truth for deployment paths.

Every Prometheus script should get its filesystem locations from here instead
of hardcoding them. Honors HERMES_HOME when set (the same contract the hermes
substrate uses), falling back to ~/.hermes.

Usage:
    from prometheus_paths import PROMETHEUS_DB, KANBAN_DB, HERMES_HOME
"""
import os

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))

PROMETHEUS_DB = os.path.join(HERMES_HOME, "prometheus.db")
KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")

SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")
ARTIFACTS_DIR = os.path.join(HERMES_HOME, "artifacts")
EXPERIMENTS_DIR = os.path.join(HERMES_HOME, "experiments")
LOGS_DIR = os.path.join(HERMES_HOME, "logs")
BACKUPS_DIR = os.path.join(HERMES_HOME, "backups")
DOCS_DIR = os.path.join(HERMES_HOME, "docs")
WORKSPACES_DIR = os.path.join(HERMES_HOME, "kanban", "workspaces")

STATE_FILE = os.path.join(HERMES_HOME, "self_state.json")
WORLD_CALIBRATION_FILE = os.path.join(HERMES_HOME, "world_calibration.json")


def under_home(*parts: str) -> str:
    """Join a path under HERMES_HOME (for one-off locations)."""
    return os.path.join(HERMES_HOME, *parts)
