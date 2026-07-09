# Director Skip Threshold Decision Point

**When to apply:** Step 2 of the Director Quick-Pass Flowchart, before checking worker availability.

## Logic

The pre-run script outputs one of two messages:
- `"SKIP: self_state.json updated Ns ago (< 90s threshold)"` → **SKIP active**
- `"OK: self_state.json updated Ns ago (≥ 90s threshold)"` → **SKIP NOT active**

## What SKIP Gates

| Task Type | SKIP Active? | Rationale |
|-----------|--------------|-----------|
| Investigation tasks (exp_NNN) | ❌ GATED | Full workflow needs fresh state |
| Synthesis tasks | ✅ ALLOWED | Maintenance, not investigation |
| Curation tasks | ✅ ALLOWED | Maintenance, not investigation |
| Stuck task reclaim | ✅ ALLOWED | Safety, not investigation |

## Director Behavior During SKIP

```
IF SKIP active:
  1. Still check board for unsynthesized experiments (step 3)
  2. Still create synthesis tasks if unsynthesized exist (maintenance)
  3. Still reclaim stuck tasks (safety)
  4. DO NOT create new experiment tasks (investigation gated)
  5. Log pass as "DIRECTOR PASS — SKIP threshold active, investigation gated"
ELSE:
  → Full workflow (steps 3-8)
```

## Common Mistake

Creating experiment tasks during SKIP threshold wastes the pre-run script's intent. The skip threshold exists because self_state.json was recently updated — reading it again immediately risks using stale/inconsistent state for new experiment creation. Synthesis tasks don't have this problem because they READ self_state.json as input (not for decision-making about what to create).

## Verified Behavior (Cycle #219)

Director pass with SKIP active:
- Created synthesis task for 2 unsynthesized experiments → ✅ correct
- Did NOT create experiment tasks → ✅ correct
- Audit log noted "SKIP threshold active" → ✅ correct
