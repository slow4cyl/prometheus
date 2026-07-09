# Stuck Worker Diagnosis Flowchart

**Automated version:** The `task-janitor` skill runs every 5 minutes and automates much of this flowchart — detecting stale workspaces, auto-completing tasks with reports, reclaiming stuck tasks, and abandoning after repeated failures. See `devops/task-janitor` for the automated postmortem system. This flowchart is the manual fallback for cases the janitor can't handle.

```
Worker shows 0 events for N minutes
            │
            ▼
    ┌───────────────┐
    │ ps aux shows  │
    │ worker process?│
    └───────┬───────┘
         NO │        YES
            ▼         ▼
    ┌──────────┐  ┌──────────────┐
    │ CRASHED  │  │ Process alive│
    │ Block    │  │ Check task   │
    │ immediately│ │ status in DB │
    └──────────┘  └──────┬───────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         Task=done   Task=running  Task=running
         ┌──────────┐  │          │
         │ZOMBIE ON │  ▼          ▼
         │DONE      │  Check     N > 120m
         │Kill proc │  workspace ┌──────────┐
         │(see ref) │  files     │Likely stuck│
         └──────────┘  ┌──────┐  │Block with  │
                       │Normal│  │specific    │
                  ┌────┤Wait  │  │reason      │
                  │    │      │  └──────────┘
             Only │    └──────┘
             script│
             + no  │
             output│
             for   │
             60+m  │
             ┌─────┴────┐
             │HUNG      │
             │Block with│
             │zero-output│
             │reason    │
             └──────────┘
```

## Why Process-Alive Matters

API-heavy experiments (multi-model evaluations, reasoning model batches) can run 10-30+ minutes with no heartbeats. The worker is genuinely computing — blocking it wastes API calls already made.

**Cycle #145 example**: exp_158 ran 122m with 0 heartbeats. `ps aux` showed prometheus-worker-7 executing a Qwen3.6 API call. NOT stuck — long evaluation. The process-alive check prevented a false positive block.

**Zombie on done task (June 2026)**: When a worker process is alive but its task is already `done` in the DB, the process is a zombie — it completed work but didn't exit cleanly. Kill it immediately. See `workers-on-done-tasks.md` for the full detection pattern and cleanup code.

## The "Alive but Silent" Pattern (Cycle #150+)

A process can be alive (`ps aux` confirms) yet completely hung — producing zero output files for 80+ minutes. The workspace contains only the original script file.

**Cycle #150+ example**: exp_214 ran 82m with 0 heartbeats. `ps aux` showed the Python process alive (PID 81940, running since 4:20AM). But workspace inspection revealed only 1 file — the original `exp_214_dispatch_fact_minimization.py` script — with zero output files and last modification at 4:17AM (81m stale). This is a HUNG process: alive but producing nothing.

**Key distinction**: A healthy long API call produces output files as it progresses (logs, intermediate results). A hung process produces nothing — only the original script exists in the workspace.

**Diagnostic**: After confirming process alive via `ps aux`, check workspace file count. If only the script exists and nothing else after 60+ minutes, the process is hung. Block it.

## "Just Dispatched" False Positive (Cycle #151)

Tasks dispatched in the current cycle show 0 files and 999m age in workspace staleness checks. This is NORMAL — the worker process hasn't started writing files yet.

**Diagnostic**: If a task is <10 minutes old and shows 0 files, check `ps aux` for the process. If the process exists, the task is healthy — just early. Wait 5+ minutes before re-checking.

**Contrast with hung process**: A hung process is alive but produces nothing for 60+ minutes. A newly-dispatched process is alive and hasn't had time to produce anything yet. Age is the discriminator: <10m = early, >60m with 0 output = hung.

## Output Logs Outside Workspace

Many experiment scripts write output to `~/.hermes/experiments/` or `/tmp/` rather than the workspace directory. The workspace staleness check (Method 3) will show these tasks as "STALE" or "DEAD" even when they're actively writing to external log files.

**Workaround**: When a task shows STALE but `ps aux` confirms the process is alive, check for output logs:
```bash
ls -lt ~/.hermes/experiments/exp_*_output.log 2>/dev/null | head -5
```
The most recent log file's modification time is the true last-activity signal.

## CPU Time as Definitive Signal (added Cycle #245)

Workspace checks can produce false positives when tasks write output to non-workspace directories (`~/.hermes/experiments/`, `/tmp/`). CPU time cuts through this ambiguity — a process with <1s CPU after 30+ minutes is hung regardless of where output lives.

**Check all processes in the tree, not just the hermes agent:**
```bash
HERMES_PID=$(ps aux | grep "kanban task $tid" | grep -v grep | awk '{print $2}' | head -1)
# Agent CPU (often 0 — it's just waiting)
ps -p $HERMES_PID -o time=
# Subprocess CPU (the real signal)
pgrep -P $HERMES_PID | xargs -I{} ps -p {} -o pid,time,args
```

**Decision matrix:**
| Agent CPU | Subprocess CPU | Verdict |
|-----------|---------------|---------|
| >5s | >5s | Healthy — actively working |
| 0-5s | >5s | Healthy — agent idle, subprocess working |
| 0-5s | 0-5s, <30m wall | Early — may still start working |
| 0-5s | 0-5s, >30m wall | HUNG — no process in tree is working |
| Process dead | — | CRASHED — block immediately |

**When to use:** When workspace checks are ambiguous (output in non-standard locations, files from prior runs, empty workspace with live process). CPU time is the tiebreaker.

## What to Log When Blocking

```
kanban_block(reason="stuck: [process dead/alive], 0 events in {N}m — [likely crash/retry loop/hung/zero-output]")
```

Include:
- Process status (dead or alive)
- Duration since last event
- Workspace file count (script only vs has output files)
- Most likely cause (crash, retry loop, hung API call, zero-output hang)

## What NOT to Do

1. **Don't re-create the same task immediately** — underlying issue (bad API key, model error, OOM) may recur
2. **Don't block at <30m** even with 0 heartbeats — could be normal long computation
3. **Don't assume "process alive = healthy"** — check workspace output files (Method 4). A process can be alive but hung.
4. **Don't ignore the pattern** — log in audit trail for tracking (is worker-7 consistently crashing?)
