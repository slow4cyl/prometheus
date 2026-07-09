# CRITICAL: Never Modify Remote Infrastructure

**Workers have FULL terminal access. They make HTTP calls. They do NOT manage infrastructure.**

## Rules Apply to SPECIFIC MACHINES, Not Globally

These rules protect specific shared resources (e.g., a shared inference server). Workers can do whatever they need on their local machine — modify files, run scripts, install packages. The rules are ONLY about the shared remote resource that other workers depend on.

**Example:** "Don't SSH to the shared server" does NOT mean "don't SSH to anything." It means don't SSH to the specific machine that other workers depend on.

## NEVER Do Any of the Following TO THE SHARED REMOTE MACHINE:
- SSH to it
- Start/stop/restart services on it
- Create scripts that modify it
- Patch source code on it
- Kill processes on it
- Delete files on it

## If the Remote Service is Unreachable:
1. Report the issue
2. Fall back to an alternative API (e.g., mimo-v2.5 via OpenRouter)
3. Do NOT try to fix it yourself

Workers that SSH to shared remote machines will crash services, waste money on fallback APIs, and destroy carefully optimized configurations.

See references/model-routing-and-shared-resources.md for the shared resources concept.
See director-loop skill references/vllm-competing-instances-pitfall.md for a real-world example.