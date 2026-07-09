# RAG Integration — IMPLEMENTED ✅

**Date:** 2026-06-02
**Status:** IMPLEMENTED AND DEPLOYED

## What Was Done

1. `batch_create_tasks.py → generate_task_body()`: RAG search instructions injected into every auto-created task body
2. `kanban-worker SKILL.md`: Full "RAG — Experiment Search" section added (lines 265-292)
3. `experiment-task-body-template.md`: RAG instructions in the template
4. Dashboard: RAG quality line chart added to Prometheus dashboard

## Architecture

## Dependency Chain (current)

```
Director prompt
  → calls batch_create_tasks.py
    → generate_task_body() builds task body
      → Worker receives task, reads body
        → Worker executes experiment
```

RAG is not mentioned at ANY point in this chain.

## Changes Required

### 1. batch_create_tasks.py — generate_task_body() (PRIMARY)

Add RAG section to every auto-created task body. Insert AFTER the METHOD
section, BEFORE GPU AVAILABLE:

```
MANDATORY — RAG CHECK (do this FIRST, before writing any code):
  python3 ~/.hermes/scripts/experiment_rag.py query "<topic keywords>" --top-k 5 --worker-id worker

  After running the query, check the results:
  - Score > 0.7: READ the top result's title and preview. If your question
    is already answered, report that instead of running a duplicate.
  - Score 0.4-0.7: Check if the topic overlaps. Build on it if relevant.
  - Score < 0.4 or server down: proceed with your own approach.
  DO NOT skip this step. 29% of recent experiments were duplicates.
```

### 2. experiment-task-body-template.md (REFERENCE)

Add RAG section to the template for manual Director task creation.

### 3. kanban-worker SKILL.md (DOCUMENTATION)

Add reference to references/rag-experiment-search.md (already done).

### 4. Director prompt — Autonomous Exploration Cycle (AWARENESS)

Add one line: "Workers have access to experiment RAG. Task bodies include
search instructions. You do NOT manage RAG."

## Pitfalls

- P1: Embedding server down → worker crash (fix: "proceed without" instruction)
- P2: query_rag() has no graceful error handling (fix: add try/except)
- P3: Workers blindly trust RAG results (fix: "read them" implies evaluation)
- P4: Concurrent queries overload server (not a real risk — nomic-embed is fast)
- P5: Task body bloat (~150 chars, negligible)
- P6: Template divergence (use same RAG section text in both)
- P7: Wrong topic extraction (instruct "<topic keywords>" not full hypothesis)

## Verification

1. dry-run batch_create_tasks.py — confirm RAG in output
2. Test RAG query manually
3. Kill server, verify graceful fallback
4. Deploy to one worker first, monitor 1 cycle

## Rollback

Revert 4 files. No data/schema/infra changes. Pure instruction injection.
