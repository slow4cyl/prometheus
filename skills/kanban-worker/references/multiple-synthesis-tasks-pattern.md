# Multiple Synthesis Tasks — Separate vs. Expand Scope

When experiments complete in batches at different times during a Director pass, the Director must decide: create a SEPARATE synthesis task, or expand scope on the existing one via `kanban_comment`?

## Decision Matrix

**Create SEPARATE synthesis task when:**
- The existing synthesis task has been running >5 min (likely already processing its original batch)
- The new experiments are from a DIFFERENT completion batch (completed at different times)
- The new experiments are from a DIFFERENT research thread than the original scope (scope mismatch — see comment-vs-new-task-flowchart.md)
- There are 3+ new experiments to synthesize (too many to add via comment)
- The existing synthesis task's body explicitly lists which experiments it covers (adding unlisted experiments via comment may confuse the worker)

**Expand scope via kanban_comment when:**
- The existing synthesis task was JUST dispatched (<2 min ago)
- The new experiments are closely related to the existing batch (same research thread)
- There are only 1-2 new experiments

## Real-World Example (Cycle #210)

5 experiments synthesized by first synthesis task (exp_690, exp_703, exp_705, exp_706, exp_707), but 3 more completed later (exp_679, exp_681, exp_708). The first synthesis task was already running and processing its batch. Creating a second synthesis task for the later batch was correct — it avoided the race condition and ensured both batches got dedicated attention. The first task completed successfully with its original 5 experiments; the second task was dispatched for the remaining 3.

## Pitfall — Mixing Batches

If the Director adds late-completing experiments to a synthesis task that's already mid-processing, the synthesis worker may not read the comment in time (race condition), OR may produce a partial synthesis that covers the original batch but misses the late additions. Separate tasks are safer when batches are temporally distinct.

## Related Pitfalls

- **Comment race condition** (kanban-worker SKILL.md): Synthesis task may complete before comment arrives
- **Expanding scope on running synthesis** (kanban-worker SKILL.md): Use kanban_comment for same-batch additions
- **Late-completing experiments** (kanban-worker SKILL.md): Add via kanban_comment when synthesis is still fresh
