# TIRITH Trailing Dot/Whitespace in Hostname Pitfall (added 2026-06-01)

## Problem

TIRITH's URL/hostname scanner can fire `tirith:trailing_dot_whitespace` when a hostname:port pattern appears in task body text near punctuation. The filter interprets things like `[HOST]:[PORT]).` as a hostname with a trailing dot.

**Example failure:**
```
hermes kanban create "title" --body "Use vLLM at [LOCAL_HOST]:[PORT]). NEVER use OpenRouter"
```
Error: `tirith:trailing_dot_whitespace` — Hostname '[HOST]:[PORT]).' has trailing dot or whitespace

**Why it fires:** TIRITH scans for URL-like patterns in command arguments. When a hostname:port appears near a closing paren and period (from normal sentence punctuation), the filter treats the entire span as a malformed hostname.

## Workaround

Restructure sentences so hostname:port patterns don't appear near punctuation that could be interpreted as part of the hostname:

**BAD:**
```
Use vLLM at [HOST]:[PORT]). NEVER use OpenRouter
```

**GOOD:**
```
Use vLLM for Qwen at the local endpoint. NEVER use OpenRouter
```

**GOOD:**
```
Use vLLM for Qwen ([HOST]:[PORT]). NEVER use OpenRouter
```
(this works because the paren is BEFORE the port, not after)

**GOOD:**
```
Use vLLM for Qwen at [HOST] port [PORT] — NEVER use OpenRouter
```

## Why This Works

TIRITH's hostname scanner looks for hostname:port patterns followed by certain terminators. When punctuation appears immediately after the port number, it gets included in the hostname match. Rephrasing to avoid the `:port)` or `:port.` pattern prevents the false positive.

## Related Pitfalls

- `tirith:raw_ip_url` — raw IP addresses in task bodies (use hostnames instead)
- `tirith:confusable_text` — Unicode math symbols in heredocs
- `tirith:variation_selector` — emoji in heredocs
- `tirith:pipe_to_interpreter` — piping to python3
- `tirith:dotfile_overwrite` — heredocs appending to dotfiles
