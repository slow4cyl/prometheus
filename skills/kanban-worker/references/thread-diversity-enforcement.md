# Thread Diversity Cap Enforcement (added 2026-06-02)

## Problem

The Director Quick-Pass Flowchart says "THREAD DIVERSITY CAP: No more than 35% of created tasks from same thread" but doesn't provide a concrete enforcement pattern. In practice, the Director creates tasks one-by-one and loses track of thread counts, ending up with 40%+ from a single thread.

## Observed Violation

In the 2026-06-02 Director pass, 2 domain-thread tasks (exp_1387, exp_1388) were created out of 5 total experiment tasks, resulting in 40% domain thread — 5pp over the 35% cap.

## Enforcement Pattern

Before creating ANY experiment task, compute current thread counts and check if adding the new task would exceed the cap:

```python
from collections import Counter

def check_thread_diversity(created_tasks, candidate_thread, max_pct=0.35):
    """
    Check if adding a task from candidate_thread would exceed the diversity cap.
    
    Args:
        created_tasks: list of thread names for already-created tasks (excluding synthesis)
        candidate_thread: thread name for the task about to be created
        max_pct: maximum allowed percentage per thread (default 35%)
    
    Returns:
        (would_exceed, current_pct, projected_pct)
    """
    total = len(created_tasks)
    if total == 0:
        return False, 0.0, 0.0
    
    counts = Counter(created_tasks)
    current_count = counts.get(candidate_thread, 0)
    current_pct = current_count / total
    
    # After adding the new task:
    projected_total = total + 1
    projected_count = current_count + 1
    projected_pct = projected_count / projected_total
    
    would_exceed = projected_pct > max_pct
    return would_exceed, current_pct, projected_pct

# Usage in Director pass:
created_threads = []  # Track as you create tasks

# Before creating each task:
candidate = "domain"  # e.g., from the scorer output
exceeds, current, projected = check_thread_diversity(created_threads, candidate)
if exceeds:
    print(f"SKIP: {candidate} would be {projected:.0%} (cap={0.35:.0%})")
    # Pick next-highest-score item from different thread
else:
    # Create the task
    created_threads.append(candidate)
```

## Integration with Director Workflow

Add this check at Step 7 (CREATE EXPERIMENT TASKS) of the Director Quick-Pass Flowchart:

```
7. CREATE EXPERIMENT TASKS (only if below saturation)
   Priority order:
     a) NO_SOURCE items (pure research, always valuable)
     b) DONE/LOW-coverage items (source completed, question open)
     c) DONE/MED-coverage items (partial coverage, lower priority)
   Never create tasks for RUNNING items
   
   NEW: Before creating each task, check thread diversity:
     - Track created_threads = [] as you create tasks
     - Before creating: check_thread_diversity(created_threads, candidate_thread)
     - If would exceed 35%: skip to next-highest-score item from different thread
     - After creating: append thread to created_threads
```

## Edge Cases

1. **Synthesis tasks don't count** — the cap applies only to experiment tasks, not synthesis/curation tasks
2. **When all high-score items are from same thread** — create the best one (33% for 3 tasks), then skip the rest and pick from other threads
3. **When free workers remain but all uncovered items exceed cap** — log the gap in audit trail, don't force-create tasks that violate diversity

## Validation

- 2026-06-02: Violation observed (40% domain, 2/5 tasks). Would have been caught by this pattern: after creating exp_1387 (domain, 1/1=100%), exp_1388 would project to 2/2=100% — clearly exceeds cap. Correct action: skip exp_1388, pick next-highest-score from different thread.
