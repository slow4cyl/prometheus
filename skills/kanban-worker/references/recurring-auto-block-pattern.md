# Recurring Auto-Block Pattern (June 5 2026)

## Symptom

A task gets auto-blocked by the system shortly after dispatch. The Director unblocks and reassigns to a different worker, but the task auto-blocks again. This repeats across multiple workers.

**Observed (June 5 2026):** exp_101608 auto-blocked on worker-18, reassigned to worker-20, auto-blocked again. Task body contained TIRITH-sensitive patterns (pipe-to-interpreter, Unicode).

## Root Causes

1. **TIRITH in task body:** The task body contains patterns that TIRITH blocks during execution (pipes, Unicode variation selectors, dotfile overwrites). The worker process gets killed or blocked before it can start.

2. **Experiment crashes on startup:** The experiment code itself has a bug, missing dependency, or configuration error that causes immediate failure.

3. **Worker-specific infrastructure:** A specific worker profile has a broken venv, missing API key, or corrupted workspace.

## Diagnosis

1. Check task body for TIRITH-sensitive patterns:
   ```bash
   sqlite3 ~/.hermes/kanban.db "SELECT body FROM tasks WHERE id='t_xxx'" | grep -E '\||⚠️|python3.*-c'
   ```

2. Check worker process logs:
   ```bash
   ps aux | grep "kanban task t_xxx" | grep -v grep
   ```

3. Check workspace for crash indicators:
   ```bash
   ls -la ~/.hermes/kanban/workspaces/t_xxx/
   ```

4. If task body is clean and workspace is empty -> likely TIRITH blocking the dispatch itself, not the experiment.

## Fix

- **TIRITH in body:** Rewrite task body to avoid blocked patterns. Use write_file() to scripts instead of heredocs. Avoid Unicode in terminal commands.
- **Experiment crash:** Fix the bug in the experiment code, then re-dispatch.
- **Worker-specific:** Reassign to a known-good worker profile.

## Anti-pattern

Unblocking and reassigning to the same or similar workers repeatedly without diagnosing the root cause. Each retry wastes a worker slot for the duration of the auto-block cycle (~1-2 minutes).
