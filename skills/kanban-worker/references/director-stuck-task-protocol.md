# Director Stuck-Task Protocol

When running as Director, you will encounter tasks that repeatedly get stuck. This protocol tells you when to reclaim vs when to block permanently.

## Decision Flowchart

```
Task shows 0 output / 0 events for N minutes
            │
            ▼
    ┌───────────────────┐
    │ How many times has │
    │ this task been     │
    │ reclaimed already? │
    └───────┬───────────┘
         0  │   1-2  │   3+
            ▼    ▼      ▼
        ┌──────┐ ┌──────┐ ┌──────────┐
        │Reclaim│ │Reclaim│ │BLOCK     │
        │once   │ │once   │ │permanently│
        │more   │ │more   │ │with      │
        │       │ │       │ │pattern   │
        └──────┘ └──────┘ │reason    │
                          └──────────┘
```

## Recurring Stuck Task Escalation (added Cycle #158)

When a task has been reclaimed 3+ times and each time shows zero output for 60+ minutes, **block it permanently** rather than reclaiming again. The underlying issue is not a transient glitch — it is a systematic failure that will not resolve with a fresh worker.

**Real-world example (Cycle #158):** exp_291 (contradictory essential facts) was reclaimed 3 times over several hours. Each time:
- Worker spawned successfully
- Process was alive (ps aux confirmed)
- Zero output files after 60+ minutes
- Zero events, zero heartbeats

After the 3rd reclaim, blocking was the correct action. Reclaiming a 4th time would just produce the same result.

**Block reason pattern for recurring stuck tasks:**
```
kanban_block(reason="Recurring stuck: reclaimed {N}x, each time zero output for {M}+ min. Needs investigation of root cause before re-attempt.")
```

**What to investigate before re-attempting:**
1. Check if the experiment script has infinite loops or deadlock-prone API calls
2. Check if the model being called is returning errors that the script does not handle
3. Check if the script depends on external resources (vLLM, database) that may be down
4. Consider rewriting the experiment from scratch with better error handling

## When to Reclaim vs Block

| Situation | Action | Why |
|-----------|--------|-----|
| First time stuck, process alive | Reclaim | Might be transient (API timeout, network blip) |
| Second time stuck, same pattern | Reclaim with caution | Could still be transient, but note the pattern |
| Third+ time stuck, same pattern | Block permanently | Systematic failure — needs root cause investigation |
| Process dead | Block immediately | Crash, not a transient issue |
| Process alive, 0 output, >120m | Block | Hung process, reclaiming will not help |

## Logging Recurring Stuck Tasks

Always log recurring stuck tasks in the audit trail for pattern tracking:

```
[2026-05-31T09:15:44Z] DIRECTOR PASS
  BLOCKED: exp_291 (recurring stuck, reclaimed 3x with zero output each time)
```

This helps identify:
- Which workers consistently crash (worker config issue?)
- Which experiment scripts are fragile (code quality issue?)
- Which API endpoints cause hangs (infrastructure issue?)
