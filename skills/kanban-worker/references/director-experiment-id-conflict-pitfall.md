# Director Experiment ID Conflict Pitfall (Cycle #209, extended Cycle #280)

## Problem

When the Director creates new experiment tasks, it may assign experiment numbers (exp_NNN) that conflict with **existing running OR completed tasks**. This produces duplicate exp_NNN prefixes, confusing synthesis and metrics tracking.

## Two Conflict Variants

### Variant 1: Conflict with RUNNING tasks (Cycle #209)

Director creates exp_692 while exp_692 is already running — two workers investigate the same number.

### Variant 2: Conflict with DONE tasks (Cycle #280)

Director creates exp_1580 while exp_1580 already completed (different experiment, same number). The done list has 1800+ tasks — easy to miss. The new task blocks itself after creation, wasting a dispatch cycle.

**Why this is worse than Variant 1:** Running task conflicts are caught by the board scan (you're already looking at running tasks). Done task conflicts require scanning 1800+ completed tasks — the max-ID approach is the only reliable defense.

## Mandatory Validation (before creating ANY experiment task)

```python
import re, json

# Step 1: Dump board state (TIRITH-safe two-step)
# hermes kanban list --status running --json > /tmp/kanban_running.json
# hermes kanban list --status done --json > /tmp/kanban_done.json

running = json.load(open('/tmp/kanban_running.json'))
done = json.load(open('/tmp/kanban_done.json'))

# Step 2: Extract ALL experiment IDs from ALL tasks (running + done)
all_exp_ids = set()
for t in running + done:
    for m in re.finditer(r'exp_(\d+)', t.get('title', '')):
        all_exp_ids.add(int(m.group(1)))

# Step 3: Use max+1 for new tasks — NEVER hardcode or guess
next_exp_id = max(all_exp_ids) + 1 if all_exp_ids else 1
print(f"Next available experiment ID: exp_{next_exp_id}")

# Step 4: Create tasks with sequential IDs
tasks = [
    {"title": f"exp_{next_exp_id}: ...", ...},
    {"title": f"exp_{next_exp_id+1}: ...", ...},
]
```

**Critical: The max-ID approach is the ONLY reliable defense.** Checking individual titles against a set is error-prone with 1800+ done tasks. Always use `max(all_exp_ids) + 1`.

## Recovery (if conflict already created)

1. Block the conflicting task: `hermes kanban block <task_id> "exp_NNN naming collision"`
2. Create replacement with correct ID: `hermes kanban create "exp_{next}: ..." --assignee ... --body ...`
3. Log the collision in audit trail for pattern tracking

## Prevention

1. **ALWAYS scan both running AND done tasks** before generating experiment IDs
2. **Use max+1 from combined set** — never hardcode or guess experiment numbers
3. **The done list is the primary hazard** — 1800+ completed experiments mean high collision probability if you don't check

## Related Pitfalls

- "Duplicate dispatch — same experiment in done + running" (existing pitfall)
- "Pitfall — same-topic different-ID duplicates" (existing pitfall)
- Variant 1: Director creates NEW tasks with IDs that conflict with EXISTING running tasks (Cycle #209)
- Variant 2: Director creates NEW tasks with IDs that conflict with EXISTING completed tasks (Cycle #280)
