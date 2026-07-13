#!/bin/bash
# calibration_loop.sh — the closed-loop tick. Runs unattended every 5 min.
#
#   1. Retrain the multivariate calibrator (champion/challenger guardrail inside
#      calibration_trainer.py — only promotes a model that BEATS the current one).
#   2. If (and only if) a new model was promoted this tick, backfill the whole
#      worker_results table so every claim reflects the newest model.
#
# Conforms to the Prometheus cron conventions (see rag_index_guard.py,
# watchdog_of_watchdogs.py):
#   - flock single-instance guard: overlapping ticks return SILENTLY (a long
#     backfill can never let two runs pile up on the DB).
#   - stamp file updated on every successful tick (freshness signal).
#   - log to ~/.hermes/logs/ ; stays SILENT on stdout in the common case so the
#     no_agent cron delivers nothing unless a promotion or error happens.
#   - watched by watchdog_of_watchdogs.py (generic >5min-stale check + the named
#     critical-jobs list + the stuck-script kill switch on calibration_trainer.py
#     / backfill_calibration_mv.py).
set -uo pipefail

HERMES="${HERMES_HOME:-$HOME/.hermes}"
VENV_PY="$HERMES/venv/bin/python"
SYS_PY="/usr/bin/python3"
SCRIPTS="$HERMES/scripts"
HIST_DB="$HERMES/prometheus.db"
LOG="$HERMES/logs/calibration_loop.log"
LOCK="$HERMES/.calibration_loop.lock"
STAMP="$HERMES/.calibration_loop_last"
mkdir -p "$HERMES/logs"

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# --- single-instance flock: if a previous tick (e.g. a long backfill) is still
#     running, exit silently. fd 9 held for the life of this process. ---
exec 9>"$LOCK"
if ! flock -n 9; then
    exit 0
fi

before=$(sqlite3 "$HIST_DB" "PRAGMA busy_timeout=15000; SELECT COUNT(*) FROM calibration_model_history WHERE promoted=1;" 2>/dev/null | tail -1 || echo "NA")

# --- 1. retrain (needs sklearn -> venv python) ---
train_out=$("$VENV_PY" "$SCRIPTS/calibration_trainer.py" 2>&1)
train_rc=$?
echo "[$(ts)] TRAIN rc=$train_rc :: $(echo "$train_out" | tail -1)" >> "$LOG"

if [ "$train_rc" -ne 0 ]; then
    echo "calibration_loop: TRAINER FAILED rc=$train_rc :: $(echo "$train_out" | tail -1)"
    exit 1
fi

after=$(sqlite3 "$HIST_DB" "PRAGMA busy_timeout=15000; SELECT COUNT(*) FROM calibration_model_history WHERE promoted=1;" 2>/dev/null | tail -1 || echo "NA")

# --- 3. Backfill only if a new model was promoted ---
if [ "$before" != "NA" ] && [ "$after" != "NA" ] && [ "$after" -gt "$before" ]; then
    # NOTE: the old step 3a ran train_calibration.py here to "fit Beta params".
    # That is a fossil — calibration_trainer.py already fits beta_a/b/c
    # (_beta_a_b_c) and writes model_current.pkl + .json IN SYNC. train_calibration
    # overwrote ONLY the .json with a second, unconstrained (non-monotone) beta fit,
    # desyncing the runtime's .json from the .pkl the promotion gate scores. The gate
    # then saw a healthy champion (.pkl) and rejected every challenger for "no gain"
    # while the runtime served a collapsed map (raw 0.95 -> ~0.0001). Removed
    # 2026-07-13; the trainer's own artifacts are authoritative. See train_calibration.py
    # (now guarded to refuse to run).

    # 3b. Backfill recalibrated confidences (sklearn-free -> sys python)
    bf_out=$("$SYS_PY" "$SCRIPTS/backfill_calibration_mv.py" 2>&1)
    bf_rc=$?
    echo "[$(ts)] BACKFILL rc=$bf_rc :: $(echo "$bf_out" | tail -1)" >> "$LOG"
    date +%s > "$STAMP"
    if [ "$bf_rc" -ne 0 ]; then
        echo "calibration_loop: model promoted but BACKFILL FAILED rc=$bf_rc :: $(echo "$bf_out" | tail -1)"
        exit 1
    fi
    metrics=$(echo "$train_out" | grep -o 'AUC [0-9.]*, Brier [0-9.]*, Res [0-9.]*' | tail -1)
    echo "calibration_loop: promoted new calibrator ($metrics) and backfilled $(echo "$bf_out" | grep -o 'DONE: [0-9]* rows' | tail -1)."

    # Dump per-domain BENCH3 accuracy for external-probe routing
    $SYS_PY "$SCRIPTS/dump_domain_accuracy.py" >> "$LOG" 2>&1

    exit 0
fi

# Common case: ran, kept champion. Update stamp, stay SILENT (no stdout).
date +%s > "$STAMP"

# Dump per-domain BENCH3 accuracy for external-probe routing
$SYS_PY "$SCRIPTS/dump_domain_accuracy.py" >> "$LOG" 2>&1

exit 0
