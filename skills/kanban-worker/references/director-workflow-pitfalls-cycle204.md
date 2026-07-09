# Director Workflow Pitfalls — Cycle #204

## Ad-hoc Script Anti-Pattern

**Problem:** Directors frequently write ad-hoc Python scripts (`/tmp/analyze_board.py`, `/tmp/classify_queue.py`, `/tmp/check_task.py`, `/tmp/health_check.py`) that duplicate what existing scripts already do.

**Solution:** Use the existing scripts in the kanban-worker skill:
- `scripts/director_queue_triage.py` — full classification + coverage scoring + unsynthesis detection
- `scripts/director_health_check.py` — bulk workspace liveness checks

Both read from `/tmp/kanban_running.json` and `/tmp/kanban_done.json`. Dump board state first with the two-step TIRITH-safe pattern, then run the script.

**Before writing any board analysis script:** Check if the existing scripts cover your use case. They almost always do. If you need a variant, patch the existing script rather than creating a new one.

## Hung Task Detection — Network Connection Check

When a task shows 0 heartbeats and 0 events but the process is alive, check if it has active network connections:

```bash
# Find the Python script process (child of hermes agent)
ps -ef | grep <task_id> | grep -v grep

# Check network connections for the script process
lsof -p <script_pid> -i 2>/dev/null | grep -E "TCP|UDP" | head -5
```

**Interpretation:**
- **Active TCP connection to API endpoint** (e.g., `2606:4700::6812:373` for Cloudflare/OpenRouter): Process is likely stuck on an API call that never returned. Wait 5-10 more minutes; if no output appears, reclaim.
- **No network connections**: Process is stuck on local I/O or computation. Check workspace files — if empty after 30+ minutes, reclaim.
- **CLOSE_WAIT state**: Connection was closed by remote but process hasn't cleaned up. Likely hung.

**Why this matters:** The `ps aux` check confirms the process is alive, but doesn't tell you WHAT it's doing. Network connection state reveals whether it's waiting on an external API (may recover) or stuck locally (won't recover).

## Timeout Command Behavior in Hermes Subprocesses

When hermes spawns a command with `timeout N`, the timeout runs in a bash subshell:

```bash
/bin/bash -c 'cd /path && timeout 180 python3 script.py > output.txt 2>&1; echo "Exit code: $?"; head -100 output.txt'
```

**Pitfall:** The `timeout 180` should kill the process after 3 minutes, but if the process is stuck in a way that prevents signal delivery (e.g., blocked on a network call in a different thread), the timeout may not work. The process continues running indefinitely.

**Detection:** If a task has been running >10 minutes and the workspace output file is empty (0 bytes), the timeout likely failed. The process is hung.

**Action:** Reclaim the task and block it with reason noting the failed timeout.

## Post-Synthesis Gap Detection (added Cycle #204)

**Problem:** The Director's initial board dump may show a synthesis task as `running` — but by the time the Director finishes queue classification and is ready to create tasks, that synthesis task may have COMPLETED. The Director proceeds with 8 running experiments (at saturation) and never creates a replacement synthesis task. The 4+ unsynthesized experiments sit unprocessed until the next Director pass.

**Detection pattern:** After the initial board dump, re-check synthesis task status BEFORE deciding whether to create a new synthesis task:

```python
import json, os

# Re-read synthesis task status (it may have completed since initial dump)
os.system('hermes kanban show <synthesis_task_id> --json 2>&1 > /tmp/synth_recheck.json')
d = json.load(open('/tmp/synth_recheck.json'))
status = d.get('task', {}).get('status', '?')
if status == 'done':
    print("Synthesis task completed mid-pass — re-run unsynthesis detection")
    # Re-run the done_exp_ids - ss_exp_ids comparison
    # Create new synthesis task if unsynthesized experiments remain
```

**Why this happens:** Synthesis workers typically complete in 3-8 minutes. A Director pass that includes queue classification, coverage scoring, and task creation can take 2-5 minutes. The window for a synthesis task to complete between the initial dump and the synthesis decision is real.

**Rule:** Always re-check synthesis task status immediately before deciding whether to create a new synthesis task. If it completed, re-run unsynthesis detection and create a fresh synthesis task for any remaining experiments. Do NOT assume the initial dump's synthesis status is still valid.
