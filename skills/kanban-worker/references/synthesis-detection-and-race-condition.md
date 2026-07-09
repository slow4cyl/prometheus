# Synthesis Task Detection & Comment Race Condition — Updated Patterns

## Synthesis Task False-Positive Detection (Cycle #642)

### Problem
The Director's board analysis uses `title.lower().startswith("synthesis:")` to detect synthesis tasks. However, experiment tasks whose titles contain "[NEW from synthesis vXXX]" in their hypothesis text get MISCLASSIFIED as synthesis tasks. In Cycle #642, 17/21 running tasks were flagged as "SYNTHESIS" because their titles matched `re.search(r'synth|consolidat', title.lower())`.

### Root Cause
Experiment titles like `exp_2476: [NEW from synthesis v535] Standalone TF-IDF+LR wins` contain the word "synthesis" as part of the curiosity source attribution, not as the task's primary purpose.

### Fix
Use `title.upper().startswith("SYNTHESIS:")` or `title.upper().startswith("CONSOLIDATE")` instead of substring matching. The prefix is authoritative — only tasks whose PRIMARY purpose is synthesis start with "SYNTHESIS:".

```python
# WRONG — matches 17 experiment tasks
is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()

# CORRECT — matches only actual synthesis tasks
is_real_synth = title.upper().startswith("SYNTHESIS:") or title.upper().startswith("CONSOLIDATE")
```

### Detection Pattern
When running the Director board analysis, always use the STARTSWITH check:
```python
for t in running:
    title = t.get("title", "")
    is_real_synth = title.upper().startswith("SYNTHESIS:") or title.upper().startswith("CONSOLIDATE")
    # Only count is_real_synth tasks for synthesis scope decisions
```

---

## Synthesis Comment Race Condition — Narrowed Window (Cycle #642)

### Previous Understanding (Cycle #246)
After dispatching a synthesis task, check event count:
- 0 events = safe to `kanban_comment` additional experiments
- 1+ events = create separate synthesis task

### What Happened in Cycle #642
1. Synthesis task t_acbada40 was dispatched
2. Checked `kanban show --json`: 0 events, 0 comments
3. Added comment: "ADDITIONAL: Also synthesize exp_2550"
4. Synthesis task completed — summary shows only exp_182, exp_811_v2, exp_2448, exp_2527
5. exp_2550 was NOT processed despite the comment being there

### New Understanding
The race window is UNBOUNDED. The synthesis worker can complete BETWEEN:
- The 0-events check
- The comment being processed by the kanban system

The comment appears in the task's comment list, but the synthesis worker may have already finished its work before reading comments.

### Revised Rule
**Do NOT rely on expanding scope via comments.** The ONLY reliable approach:
1. If the experiment wasn't in the original task body, create a SEPARATE synthesis task
2. Comments are for context/notes, not for scope expansion on fast-completing tasks
3. Synthesis workers complete in <2 minutes — the comment-processing latency is comparable

### When Comments Still Work
- Task has been running 5+ minutes (worker is past initial setup)
- Task has 1+ heartbeat events (worker is actively processing)
- Task body explicitly says "read comments for additional experiments"

### When to Create Separate Tasks
- Task was just dispatched (<2 min old)
- Task has 0 events
- Task body doesn't mention reading comments
- The experiment is critical and can't risk being missed
