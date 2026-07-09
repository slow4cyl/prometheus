# Workers Must Never Modify Remote Infrastructure

## The Rule

Kanban workers have FULL terminal access for local scripts. They do NOT manage infrastructure (no SSH to remote servers).
Kanban workers have FULL terminal access for local scripts. They do NOT manage infrastructure (no SSH to remote servers).
Workers must NEVER:
- SSH to remote machines (any servers)
- Start/stop/restart services (vLLM, databases, etc.)
- Create scripts that modify remote systems
- Patch source code on remote machines
- Kill processes on remote machines

Workers MUST:
- Make HTTP API calls to inference endpoints
- Use the served model name (e.g., `Qwen3.6-35B-A3B`), NOT file paths
- Fall back to alternative APIs if the primary is unreachable
- Report infrastructure issues, not fix them

## Why This Matters

In June 2026, Prometheus workers were SSHing to remote inference servers and launching their own vLLM instances with different configurations. This caused:
1. GPU memory fights between competing instances
2. Both instances crashing repeatedly
3. $52 burned in 15 minutes via OpenRouter fallback calls
4. Weeks of carefully optimized vLLM config (MTP, 0.88 util, flashinfer) being overwritten

## The Pattern

Workers generate Python scripts that call LLM APIs. The scripts should contain:
- HTTP POST to `http://HOST:PORT/v1/chat/completions`
- Model ID as served name (not file path)
- Timeout handling with fallback

The scripts should NEVER contain:
- `ssh` commands
- `subprocess.run` with SSH
- `vllm serve` commands
- `pkill` commands
- File writes to remote systems

## Infrastructure Protection

When creating worker tasks, always include:
```
INFRASTRUCTURE RULE: Use HTTP API calls only. NEVER SSH to [HOST]. NEVER start/stop services. If endpoint is unreachable, fall back to [ALTERNATIVE].
```

This rule must be in the task description, not just in SKILL.md — workers generate code from task descriptions.
