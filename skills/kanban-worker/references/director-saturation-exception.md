# Director Saturation Exception — Genuinely Open Queue Items

**Added:** Cycle #201
**Category:** Director saturation threshold exception

## Problem

The standard saturation rule says "≥7 running: SYNTHESIZE only — do NOT create new experiment tasks." But this wastes free workers when genuinely uncovered high-value research questions exist in the queue.

## Exception Rule

When ALL of these conditions are met, creating tasks is valid even during saturation (≥7 running):

1. **free_workers >= 3** — enough workers to justify the assessment overhead
2. **3+ genuinely OPEN queue items** — items with LOW coverage (<0.15) against running task titles
3. **Items are independent** — no prerequisites that aren't already queued

## What counts as "genuinely OPEN"

- **NO_SOURCE items** with LOW coverage (<0.15): Pure research questions not referenced by any running task
- **DONE items** whose specific question is NOT covered by any running experiment: Source experiment completed, but the queue item asks a follow-up question that no running task addresses

## What does NOT count

- **RUNNING items**: Source experiment still running — will resolve naturally
- **DONE items** with HIGH coverage (>0.3): Already covered by running tasks
- **NO_SOURCE items** with HIGH coverage (>0.3): Topic already addressed by running tasks
- **NO_SOURCE items that are ops/infrastructure tasks**: Items about workspace archival, logging, dashboard fixes, etc. are operational work, not research experiments. They require different worker types and don't produce research findings. Do NOT count them toward the 3-item threshold. Example: "Workspace retention 1.1% at scale — should we implement explicit workspace archival?" is LOW coverage (0.00) but is an ops task, not a research experiment. (Observed Cycle #242: 6 NO_SOURCE items scored, but only 2 were genuine research questions after excluding ops items.)

## Verification steps

1. Run queue classification (use `director_queue_triage.py` script)
2. Check coverage scores for NO_SOURCE and DONE items
3. Count items with LOW coverage (<0.15)
4. If count >= 3 AND free_workers >= 3, create tasks for the LOW-coverage items
5. If count < 3, stay in synthesis-only mode

## Real-world example (Cycle #201)

- Board: 15 running tasks (saturation mode)
- Free workers: 8
- Queue: 37 DONE items, 3 DIRECTOR items
- After coverage scoring: 4 NO_SOURCE items with LOW coverage + 3 DIRECTOR items = 7 genuinely open items
- Created 7 tasks, dispatched to 7 free workers
- Result: 19 running tasks, 3 free workers remaining

## Synthesis Threshold Refinement (added Cycle #242)

The standard rule says "≥3 unsynthesized → CREATE SYNTHESIS TASK". However, a single high-value unsynthesized experiment warrants immediate synthesis if it has:
- Novel mechanistic findings (e.g., AUC=1.0000, new feature class discovered)
- Strong hypothesis support/refutation with practical implications
- Findings that unblock other research directions

In Cycle #242, exp_1334 (ensemble methods for TF-IDF hard floor) showed contradiction probing features push AUC from 0.985 to 1.0000 — a breakthrough finding. Even though only 1 experiment was unsynthesized, synthesis was created immediately because the finding changes the research trajectory.

**Rule**: Don't wait for ≥3 unsynthesized when a single experiment has high-impact results. The synthesis worker handles 1-item tasks efficiently (<2 min).

## Anti-pattern

Do NOT apply the exception when:
- All open items have HIGH coverage (>0.3) — they're already covered by running tasks
- free_workers < 3 — assessment overhead exceeds parallelism gain
- Open items have dependencies on unfinished prerequisites

In these cases, stay in synthesis-only mode and wait for saturation to clear.
