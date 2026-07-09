# Worker Behavior and Infrastructure Management (June 2026)

## The Core Problem: Workers Don't Understand Shared Infrastructure

Prometheus workers are autonomous agents that optimize locally without understanding global impact. When a worker sees an endpoint or machine name, it generates code to interact with it — often SSHing and launching its own services. This causes:

- GPU memory fights between competing instances
- Both instances crashing
- Expensive fallback calls ($52 burned in 15 minutes via OpenRouter)
- Weeks of carefully optimized config being overwritten

**Workers follow patterns, not understanding.** The concept of "shared resource" doesn't exist in their mental model. They optimize for their own experiment without realizing other workers depend on the same infrastructure.

## What Actually Works

### 1. Infrastructure-Level Protection (Most Effective)
- **Disable SSH password auth** on shared machines (workers use password auth, admin uses key auth)
- **Delete models workers shouldn't use** (they can't launch what doesn't exist)
- **Only one model on the machine** (anything else OOMs)

### 2. Clear Task Descriptions (Medium Effective)
- "Use the API endpoint" is better than "use the machine"
- Workers need to know the endpoint exists for experiments that need it
- But they need explicit instructions: HTTP API calls only, no SSH, no modifications

### 3. SKILL.md Rules Alone (Least Effective)
- Workers read task descriptions, not skill files
- Even explicit "NEVER SSH" rules get bypassed
- Workers generate code from task descriptions, not from skill rules

## Task Description Patterns

### Bad (Causes SSH)
"Use Qwen3.6-35B-A3B on the local vLLM server at http://[LOCAL_HOST]:[PORT]/v1/chat/completions"

### Good (API Only)
"Use Qwen3.6-35B-A3B via API at http://[LOCAL_HOST]:[PORT]/v1/chat/completions with model ID Qwen3.6-35B-A3B"

### Better (No Infrastructure Context)
"Use mimo-v2.5 for this experiment" (when Qwen isn't needed)

## The Machine Name Problem

When task descriptions mention machine names, workers interpret them as machines they need to manage. They SSH to them, launch competing vLLM instances, crash servers, and burn budget on fallback calls.

**Solution:** Don't mention machine names in task descriptions. Just provide the API endpoint URL and model ID. Workers make HTTP calls. That's the entire interaction.

## Fallback Pattern

When a shared resource is unreachable:
1. Fall back to the default model (mimo-v2.5)
2. Do NOT fall back to expensive alternatives ($1/M OpenRouter Qwen)
3. Log the fallback for monitoring
4. Report the issue, don't try to fix it

## Worker Psychology

Workers are language model agents. They:
- Follow patterns from previous scripts
- Optimize locally without global awareness
- Generate code from task descriptions
- Don't distinguish between "HTTP to endpoint" and "SSH to machine"
- See "machine name" and think "I need to manage this"

The fix is clear task descriptions that specify exactly what to do (HTTP API calls) without providing the mechanisms that enable damage (SSH, vLLM management, file modifications).
