"""
worker_config.py — SINGLE SOURCE OF TRUTH for worker fleet sizing and the
two-lane (genuine/transfer) task-creation split.

This is THE dial. To change how many workers the Director loop drives, edit
WORKER_COUNT below (or set the PROMETHEUS_WORKER_COUNT env var). Everything
else — the per-cycle task target, the 60/40 genuine/transfer split, and the
backlog buffer — is derived from it automatically, so the dual-lane ratio
always scales to the fleet size.

Consumed by:
  - director.py          (builds LANE_PLAN from lane_plan())
  - batch_create_tasks.py (MAX_WORKERS / WORKERS list)

"""
import os

# ── THE DIAL ────────────────────────────────────────────────────────────────
# All workers now use the 'default' profile. max_in_progress_per_profile=20
# allows 20 concurrent tasks on one profile.
WORKER_COUNT = int(os.environ.get("PROMETHEUS_WORKER_COUNT", "20"))

# DEEP READY-QUEUE model. Goal: keep a large overflow of staged 'ready' tasks
# so the gateway always has work to spawn and all WORKER_COUNT workers stay
# busy. The gateway dispatches ready tasks every kanban.dispatch_interval_seconds
# and enforces kanban.max_in_progress_per_profile=20 on the default profile.
READY_TARGET_DEPTH = int(os.environ.get(
    "PROMETHEUS_READY_TARGET_DEPTH", str(int(WORKER_COUNT * 0.5))))

# Max tasks creation attempts in ONE cycle (creation is slow: dedup vs ~15.8k
# experiments). The pool keeps growing across cycles toward READY_TARGET_DEPTH;
# this bounds per-cycle work so a single pass doesn't run for many minutes.
# Default = WORKER_COUNT (a full fleet's worth of new staging per cycle).
MAX_CREATE_PER_CYCLE = int(os.environ.get("PROMETHEUS_MAX_CREATE_PER_CYCLE", str(WORKER_COUNT)))

# Genuine-lane share of each creation batch. 0.60 = the 60/40 split.
# Transfer lane gets the remainder. The two lanes never compete for slots.
GENUINE_SHARE = float(os.environ.get("PROMETHEUS_GENUINE_SHARE", "0.60"))

# All workers use the 'default' profile now
WORKER_PROFILE = "default"
# ─────────────────────────────────────────────────────────────────────────────


def worker_list(count: int | None = None) -> list[str]:
    """Profile names for the fleet. All workers use 'default' profile."""
    return [WORKER_PROFILE]


def create_budget(current_ready: int = 0) -> int:
    """How many tasks to create THIS cycle to top the ready pool toward depth.

    = clamp(READY_TARGET_DEPTH - current_ready, 0, MAX_CREATE_PER_CYCLE).
    When the pool is already deep, returns 0 (don't overproduce). When it's
    drained, returns up to MAX_CREATE_PER_CYCLE to refill it.
    """
    deficit = max(0, READY_TARGET_DEPTH - max(0, current_ready))
    return min(deficit, MAX_CREATE_PER_CYCLE)


def cycle_target(count: int | None = None) -> int:
    """Back-comat alias: per-cycle creation cap when pool depth is unknown.

    Equivalent to a full top-up from empty, bounded by MAX_CREATE_PER_CYCLE.
    Prefer create_budget(current_ready) when the live ready count is available.
    """
    return MAX_CREATE_PER_CYCLE


def lane_plan(count: int | None = None, current_ready: int | None = None) -> list[tuple[str, int]]:
    """Two-lane plan [("genuine", g), ("transfer", t)] for this cycle's batch.

    Sizes to create_budget(current_ready) (top-up toward READY_TARGET_DEPTH),
    or to MAX_CREATE_PER_CYCLE when current_ready is unknown. Split 60/40 via
    GENUINE_SHARE. Returns [("genuine",0),("transfer",0)] when the pool is full.
    """
    if current_ready is not None:
        target = create_budget(current_ready)
    elif count is not None:
        target = count
    else:
        target = MAX_CREATE_PER_CYCLE
    if target <= 0:
        return [("genuine", 0), ("transfer", 0)]  # pool full — create nothing
    genuine = round(target * GENUINE_SHARE)  # round, not floor — genuine (60%) must
                                              # get the majority; flooring handed the
                                              # remainder to transfer and flipped the
                                              # split backwards at small budgets
                                              # (target=3 gave genuine=1, transfer=2).
    # Guarantee both lanes get at least 1 slot when target >= 2.
    if target >= 2:
        genuine = min(max(genuine, 1), target - 1)
    transfer = target - genuine
    return [("genuine", genuine), ("transfer", transfer)]


if __name__ == "__main__":
    # Quick self-report: `python3 worker_config.py`
    print(f"WORKER_COUNT          = {WORKER_COUNT}")
    print(f"WORKER_PROFILE        = {WORKER_PROFILE}")
    print(f"READY_TARGET_DEPTH    = {READY_TARGET_DEPTH}   (standing ready-pool target)")
    print(f"MAX_CREATE_PER_CYCLE  = {MAX_CREATE_PER_CYCLE}")
    print(f"GENUINE_SHARE         = {GENUINE_SHARE}")
    print("\nPer-cycle plan as the ready pool fills (top-up model):")
    for ready_now in (0, WORKER_COUNT, READY_TARGET_DEPTH - 5, READY_TARGET_DEPTH):
        plan = lane_plan(current_ready=ready_now)
        g = dict(plan)["genuine"]; t = dict(plan)["transfer"]; tot = g + t
        budget = create_budget(ready_now)
        if tot:
            print(f"  ready={ready_now:3d} -> create {budget:2d}  "
                  f"genuine {g}/{tot}={g/tot:.0%} transfer {t}/{tot}={t/tot:.0%}")
        else:
            print(f"  ready={ready_now:3d} -> create  0  (pool full — skip)")
