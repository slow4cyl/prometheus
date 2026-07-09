# Director Synthesis Race Condition — Remediation Flow (Cycle 245)

## Problem

When the Director creates a synthesis task and then discovers additional experiments
completed during the same pass, adding them via `kanban_comment` often fails because
the synthesis worker completes in <2 minutes — faster than the comment arrives.

## Detection

After adding a comment to a synthesis task, immediately check `kanban show --json`
for status. If `status=done`, the comment was too late.

## Remediation Flow

1. Verify synthesis task status via `kanban show --json` — if `status=done`, comments were lost
2. Re-run unsynthesized detection (Method A) against refreshed `self_state.json`
3. Create a SECOND synthesis task for missed experiments, assigned to `prometheus-synthesis`
4. Dispatch immediately — do NOT try to re-open or comment on the completed task
5. Log both synthesis tasks in the audit trail

## Concrete Example (Cycle 245)

- Director created synthesis t_dbe47ac2 for exp_1437/1475/1477
- During pass, exp_1476/1478/1479 also completed
- Comments added to t_dbe47ac2, but it had already completed (3 experiments synthesized)
- Re-check revealed 3 remaining unsynthesized experiments
- Created second synthesis t_88785540 for the gap
- Total: 2 synthesis tasks, all experiments covered

## Anti-pattern

Attempting to re-open a completed synthesis task or create a task that covers
experiments the first task already processed. The second task should ONLY cover
experiments NOT in the first task's output.

## Prevention

After dispatching a synthesis task, the Director should NOT attempt to expand its
scope via `kanban_comment` in the same pass. Always create a separate synthesis
task for any experiments discovered after dispatch.

## NO_SOURCE Queue Item Coverage Filtering (Cycle 245)

When classifying queue items, NO_SOURCE items (no experiment ID reference) are
candidates for new tasks — but they can overlap topically with running experiments.

### Technique

Compute keyword overlap between each NO_SOURCE item's text and all running task titles:

```python
stop = {'the', 'a', 'an', 'is', 'are', ...}  # standard stop words
keywords = {w for w in words - stop if len(w) > 3 and not re.match(r'^exp_\d+', w)}
running_titles_text = ' '.join(t.get('title', '').lower() for t in running)
overlap = sum(1 for w in keywords if w in running_titles_text)
coverage = overlap / max(len(keywords), 1)
# HIGH >0.3 (likely covered), MED 0.15-0.3 (partially), LOW <0.15 (open)
```

### Results (Cycle 245)

Of 6 NO_SOURCE queue items, coverage scoring filtered out 5 as HIGH/MED (covered by
running experiments). Only 1 item (confidence-gated fusion extension) had LOW coverage
and was genuinely uncovered. This prevented creating 5 wasted experiment tasks.

### Rule

After classifying NO_SOURCE items, run quantitative coverage scoring against running
task titles. Only create tasks for NO_SOURCE items with LOW coverage (<0.15).
