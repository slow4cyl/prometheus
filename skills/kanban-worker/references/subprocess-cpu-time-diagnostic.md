# Subprocess CPU Time Diagnostic — Critical Refinement

**Added:** Cycle #218
**Category:** Stuck worker detection refinement

## Problem

When checking if a worker is stuck, the standard "CPU time diagnostic" checks the hermes agent process's CPU time. However, when a worker dispatches a Python script via `terminal(background=true)`, the hermes agent process has low CPU because it's waiting for the subprocess. The subprocess (the actual experiment script) is what's hung.

## Example (Cycle #218)

```
hermes agent (PID 91817):
  - Status: alive, 59m wall-clock
  - CPU time: 20.43s (appears healthy)
  - Reality: waiting for subprocess, not doing experiment work

spawned Python subprocess (PID 16346):
  - Status: alive, 2:48 wall-clock
  - CPU time: 0.01s (THIS is the hung process)
  - Reality: stuck on API call that never returned
```

## Detection Pattern

```bash
# Step 1: Find the subprocess spawned by the hermes agent
ps aux | grep '<script_name>.py' | grep -v grep

# Step 2: Check the subprocess's CPU time
ps -p <subprocess_pid> -o pid,etime,time,%cpu

# Step 3: If CPU time < 1s after 20+ minutes wall-clock → HUNG
```

## Why This Matters

The hermes agent's CPU time is misleading — it's just the overhead of running the agent loop, not the experiment work. A healthy hermes agent shows 20-30s CPU time regardless of whether the subprocess is working or hung.

The subprocess's CPU time tells you if it's actually doing work:
- **Healthy**: Steady CPU accumulation (30-120s per API call)
- **Hung**: <1s CPU after 20+ minutes (stuck on API call or I/O)

## Fix

When the hermes agent has 0 heartbeats but is alive:
1. Don't rely on the hermes agent's CPU time
2. Find the subprocess via `ps aux | grep '<script_name>'`
3. Check the subprocess's CPU time
4. If zero CPU after 20+ minutes → reclaim and block

## "Zero CPU Everywhere" Pattern (Cycle #245)

A more extreme variant: BOTH the hermes agent AND all subprocesses show 0 CPU time after 300+ minutes wall-clock. This is a definitive hung-process signal — no process in the tree is doing work.

**Observed in Cycle #245:** 16 tasks running 300+ minutes. All hermes agent PIDs showed 00:00:01 CPU. All child bash processes showed 00:00:00 CPU. Workspace files were empty or contained only the original script. Some tasks had output in `~/.hermes/experiments/` but those files were also stale (last modified hours ago).

**Diagnostic sequence:**
```bash
# Step 1: Find ALL processes for the task (hermes agent + children)
HERMES_PID=$(ps aux | grep "kanban task $tid" | grep -v grep | awk '{print $2}' | head -1)
echo "Hermes agent PID: $HERMES_PID, CPU: $(ps -p $HERMES_PID -o time=)"

# Step 2: Check children
pgrep -P $HERMES_PID 2>/dev/null | while read child; do
  echo "Child $child: CPU=$(ps -p $child -o time=) CMD=$(ps -p $child -o args= | head -c 80)"
done

# Step 3: Check workspace freshness vs process start time
# If workspace files predate the process start → stale artifacts from prior run
# If workspace is empty → task wrote nothing in its entire lifetime

# Step 4: If ALL processes show <1s CPU after 30+ min → HUNG
# Action: kill -9 all PIDs, reclaim, block
```

**Why this matters:** The standard "alive but silent" check (process exists + workspace has only script) catches cases where the workspace has the script but no output. The "zero CPU everywhere" pattern catches a broader set: tasks that wrote output to non-workspace paths, tasks where the output was from a prior run, and tasks where the process tree has multiple layers of zombie wrappers.

**Kill sequence for multi-process tasks:**
```bash
# Kill children first (they may hold resources)
pgrep -P $HERMES_PID 2>/dev/null | xargs -r kill -9
# Then kill the hermes agent
kill -9 $HERMES_PID
# Then reclaim + block
hermes kanban reclaim $tid
hermes kanban block $tid "hung: zero CPU on all processes for N hours"
```

**Critical: block AFTER reclaim.** If you kill without blocking, dispatch re-spawns the task on the next tick. The block prevents re-dispatch of a task with an underlying issue.

## Integration with Existing Protocol

This refines the "CPU time diagnostic (added after exp_448)" in the main SKILL.md. The existing diagnostic is correct but checks the wrong process. Always check the subprocess when the worker dispatches scripts via `terminal(background=true)`.

The "zero CPU everywhere" pattern is the nuclear option — when no process in the tree shows meaningful CPU, the task is definitively hung regardless of workspace state or output location.
