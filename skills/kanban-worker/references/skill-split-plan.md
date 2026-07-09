# kanban-worker SKILL.md Split Plan

## Problem
SKILL.md is 101,722 characters (limit: 100,000). Cannot patch or edit.

## Action Required
Move 2-3 large sections to `references/` files to get under the limit.

## Recommended sections to extract

### 1. "Director Synthesis workflow (concrete steps)" → `references/director-synthesis-workflow.md`
This section is ~4KB and is self-contained. It covers:
- Reading completed task summaries
- Updating self_state.json
- Curating the queue
- Updating knowledge graph
- Logging to audit trail

### 2. "Stuck Worker Protocol" → `references/stuck-worker-protocol.md`
This section is ~3KB and is self-contained. It covers:
- Process liveness check
- Workspace output check
- Duration assessment
- Block if stuck

### 3. "Director: batch task creation pattern" → already has references, but the inline code blocks are large

## After splitting
- Add one-line pointers in SKILL.md to each new reference file
- Apply the TIRITH curl-pipe and synthesis detection patches from `references/director-pass-pitfalls-2026-06-03.md`
- Bump version to 2.25.0

## Priority
HIGH — blocks all future patches to kanban-worker SKILL.md.
