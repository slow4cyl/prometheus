# Per-Profile Cap (max_in_progress_per_profile)

## Problem (June 2026)
The kanban dispatcher has a `max_in_progress_per_profile` setting that limits
how many tasks each worker profile can run simultaneously. When set to `null`
(the default), there is NO limit — the dispatcher assigns unlimited tasks to
the same profile, creating N processes for 1 profile.

Observed: worker processes exceeded profile count (e.g. 50 processes for 22 profiles). Worker-1 had 5 simultaneous
tasks, Worker-3 had 4. Each assignment spawns a NEW hermes process.

## Impact
- Memory explosion: 50 processes × 170MB = 8.5GB just for worker processes
- CPU oversubscription: load average 116+ on 32 cores
- Stuck processes: old tasks with 1.7s CPU after 93 minutes (zombie workers)
- API waste: multiple workers making redundant calls for same profile

## Fix
Set `kanban.max_in_progress_per_profile: 1` in `~/.hermes/config.yaml`:

```yaml
kanban:
  max_in_progress_per_profile: 1
```

**CRITICAL: Gateway must restart to pick up config changes.** The gateway reads
config at startup. Changing the config file has no effect until restart:

```bash
systemctl --user restart hermes-gateway.service
```

## Verification
```bash
# Check config
grep "max_in_progress" ~/.hermes/config.yaml

# Check for duplicates (should show max 1 per profile)
ps aux | grep "prometheus-worker" | grep -v grep | grep "chat" | \
  grep -oP "prometheus-worker-\d+" | sort | uniq -c | sort -rn
```

## Scaling Implications
With `max_in_progress_per_profile: 1`, scaling is safe:
- Add more `prometheus-worker-N` profiles
- Each gets exactly 1 task at a time
- No pile-ups, no duplicate processes
- Memory: ~170MB per worker process + 1-2GB per experiment script

## Cleanup After Enabling
If duplicates exist when enabling the cap:
1. Kill extras: `kill -9` on duplicate PIDs
2. Reclaim orphaned tasks: `hermes kanban reclaim <task_id>`
3. Let dispatcher re-dispatch with cap active

## Deferred Task Reassignment Pattern (Cycle #244.5)

When the dispatcher defers a task due to per-profile cap, the task stays in
`ready` status with the capped worker as assignee. It will NOT auto-dispatch
when the worker becomes free (the dispatcher only re-checks at dispatch time).

**Detection:** `hermes kanban dispatch` output includes:
```
Deferred (prometheus-worker-3 at per-profile cap, 1 running): t_3ec54899
```

**Update (2026-06-05):** The dispatcher now has automated reassignment built in.
Deferred tasks are automatically reassigned to a free worker when one becomes
available. See kanban-orchestrator/references/dispatch-reassignment.md for
details on the automated reassignment logic.

**Manual fallback (if automation doesn't trigger):**
```bash
# Reassign to a free worker (check ps aux for free workers first)
hermes kanban assign t_3ec54899 prometheus-worker-15
# Dispatch again to spawn the reassigned task
hermes kanban dispatch
```

**Why not wait:** The deferred task blocks the queue slot. If you're creating
new tasks in the same pass, the deferred task competes with new tasks for
dispatch slots. Reassigning immediately ensures all tasks get dispatched in
one pass instead of requiring multiple dispatch rounds.

**Prevention:** When creating tasks in a Director pass, check `ps aux` for
free workers BEFORE assigning. If the target worker is at cap, assign to a
different free worker from the start.

## Code Location
- Cap check: `hermes-agent/hermes_cli/kanban.py` lines 5977-5980
- Config read: `_kanban_cfg.get("max_in_progress_per_profile")`
- Skipped tasks reported as: `skipped_per_profile_capped`
