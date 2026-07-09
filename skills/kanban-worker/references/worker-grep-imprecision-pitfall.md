# Imprecise Worker Grep Pattern (Cycle #251)

## The Problem

The standard worker-count pattern matches non-kanban processes:

```bash
# IMPRECISE — matches ANY process with "prometheus-worker" in the command line
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
```

This returns profiles from:
- Actual kanban-dispatched worker processes
- The Director process itself (if it includes a worker profile name)
- Monitoring scripts or other hermes processes
- Zombie processes from completed tasks

In Cycle #251, this returned 9 profiles but only 8 were actually running kanban tasks. The extra match was the Director process.

## The Fix

Anchor on `'kanban task'` to filter only actual kanban-dispatched workers:

```bash
# PRECISE — only matches processes running kanban tasks
ps aux | grep 'kanban task' | grep 'prometheus-worker' | grep -oE 'prometheus-worker-[0-9]+' | sort -u
```

## Impact

Off-by-1 or off-by-2 in the free worker count can cause:
- Director creates one fewer task than available slots (wastes capacity)
- Director incorrectly applies the saturation threshold (creates tasks when it shouldn't, or vice versa)

## Detection

If the grep count exceeds the running task count from `hermes kanban list --status running --json`, the excess are non-kanban processes. Cross-reference to verify.

---

## Substring Matching False Positive (added Cycle #245)

A distinct problem from the above: when checking individual worker processes by number, `grep "prometheus-worker-3"` matches ALL processes containing that substring — including `prometheus-worker-30`, `prometheus-worker-31`, `prometheus-worker-32`, etc.

### Example (Cycle #245)

```bash
# WRONG — "prometheus-worker-3" matches worker-30, 31, 32, ... 39
ps aux | grep "prometheus-worker-3" | grep -v grep | wc -l
# Returns: 15 (but worker-3 only has 1 process!)

# CORRECT — use word boundary anchor [^0-9]
ps aux | grep -E "prometheus-worker-3[^0-9]" | grep -v grep | wc -l
# Returns: 1 (correct)
```

This false positive caused a phantom zombie alarm: Worker-3 appeared to have 15 processes when it actually had 1. The Director spent time investigating a non-existent zombie accumulation.

### Fix

When checking a specific worker number, anchor the match with `[^0-9]`:

```bash
# For any specific worker N:
ps aux | grep -E "prometheus-worker-${N}[^0-9]" | grep -v grep | wc -l

# For bulk counting (already safe — extracts all tokens independently):
ps aux | grep 'prometheus-worker' | grep -v grep | grep -oE 'prometheus-worker-[0-9]+' | sort -u
```

The bulk pattern (`grep -oE 'prometheus-worker-[0-9]+'`) is safe because `-oE` extracts each match independently from each line. The per-worker pattern needs the `[^0-9]` anchor to prevent substring matches.

### Impact

- False zombie detection wastes Director time investigating phantom worker accumulation
- In Cycle #245, Worker-3 appeared to have 15 processes, Worker-2 had 18 — both were false alarms from substring matching. Actual counts: Worker-3=1, Worker-2=2.
