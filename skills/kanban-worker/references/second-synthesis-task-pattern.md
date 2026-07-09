# Second Synthesis Task for Remaining Experiments (Cycle #245)

## When This Pattern Applies

The running synthesis task covers a SUBSET of unsynthesized experiments. The remainder need a second synthesis task. This is distinct from "expanding scope" (which adds experiments to the SAME synthesis task via kanban_comment).

## Decision Criteria

| Situation | Action | Why |
|-----------|--------|-----|
| Synthesis just spawned (0 events) | `kanban_comment` to add experiments | Worker hasn't started processing yet — it will see the comment |
| Synthesis running (1+ events, heartbeats active) | Create second synthesis task | Worker may already be processing — comment might arrive too late (race condition, Cycle #188) |
| Synthesis covers ALL unsynthesized | No action needed | First synthesis handles everything |
| Synthesis covers subset, remainder ≥3 | Second synthesis task | Remainder is large enough to justify a separate task |

## Worked Example (Cycle #245)

**Board state:**
- 51 total unsynthesized experiments (done_exp_ids − ss_exp_ids)
- Running synthesis (t_3d7c88cc): covers 27 experiments (listed in body + comment)
- Remaining: 24 experiments NOT in the running synthesis

**Step 1: Compute coverage gap**
```python
# Synthesis task covers these (from body + comment):
synthesis_covered = {3053, 3061, 3064, 3067, 3069, 3071, 3074, 3075, 3076, 3077, 3078,
                     3080, 3081, 3082, 3083, 3084, 3085, 3090, 3092,
                     2986, 3005, 3015, 3086, 3087, 3093, 3095, 3096}

unsynth = done_exp_ids - ss_exp_ids  # 51 total
not_covered = unsynth - synthesis_covered  # 24 remaining
```

**Step 2: Create second synthesis task**
```bash
hermes kanban create "SYNTHESIS: consolidate exp_2973,2993,2994,..." \
  --assignee prometheus-synthesis \
  --body "SYNTHESIS TASK: Consolidate results from 24 unsynthesized experiments...

UNSsynthesized experiments to consolidate:
exp_2973, exp_2993, exp_2994, exp_3051, exp_3068, exp_3070, exp_3079,
exp_3094, exp_3097-3100, exp_3104, exp_3105, exp_3111, exp_3113-3117,
exp_3119, exp_3120, exp_3124

[standard synthesis instructions...]"
```

**Step 3: Per-profile cap handles sequencing**
The second synthesis task is created as `ready` but deferred (prometheus-synthesis at per-profile cap, 1 running). It auto-dispatches when the first synthesis completes. No manual sequencing needed.

## Why NOT Use kanban_comment Here

The existing "Expanding scope" pattern (Cycle #159) says to use kanban_comment. But that assumes:
1. The synthesis worker hasn't started processing yet (0 events)
2. The comment will be read before processing begins

When the synthesis is actively running (1+ events, heartbeats), the worker may already be reading self_state.json and processing experiments. A comment added at this point arrives AFTER the worker has started — it may or may not be seen, depending on timing (the "comment race condition" from Cycle #188).

Creating a second synthesis task is deterministic: it will process exactly the experiments listed in its body, with no race condition.

## Overlap Check

Before creating the second synthesis task, verify zero overlap with the first:

```python
# If overlap exists, the same experiment gets synthesized twice
# (harmless — synthesis is idempotent — but wastes worker time)
overlap = synthesis_covered.intersection(not_covered)
if overlap:
    print(f"WARNING: {len(overlap)} experiments covered by both: {overlap}")
```

In Cycle #245: zero overlap (27 covered + 24 uncovered = 51 total).

## Prevention: List Experiments in Synthesis Task Body

Always list the SPECIFIC experiment IDs in the synthesis task body, not just "consolidate unsynthesized experiments." This makes coverage computation trivial:

```python
# Extract experiments from synthesis task body
body_experiments = set()
for m in re.finditer(r'exp_(\d+)', synthesis_body):
    body_experiments.add(int(m.group(1)))

# Compute uncovered
unsynth = done_exp_ids - ss_exp_ids
covered = unsynth.intersection(body_experiments)
uncovered = unsynth - body_experiments
```

Without explicit IDs in the body, the Director must reverse-engineer coverage from the synthesis task title (unreliable — titles often truncate experiment lists).
