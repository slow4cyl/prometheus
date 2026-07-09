# Director Pass — Audit Log Template

Every Director pass should append a structured entry to `~/.hermes/self_audit.log` with these sections:

```
=== DIRECTOR PASS — <ISO timestamp> ===
Board: <N> running, <N> done, <N> ready
Workers: <N> active (<list free workers>)
Unsynthesized: <N> (<exp IDs or "none") — <synthesis decision>
Queue: <N> active items, <coverage summary>

Actions taken:
1. <action> — <result>
2. ...

Queue coverage analysis:
  [N] <topic> — <COVERED/PARTIAL/OPEN> (<running exp or "none">)

Next cycle priority: <what to do next>
```

## Why this format

- **Scannable**: The Board/Workers/Unsynthesized header gives instant situational awareness
- **Traceable**: Actions taken section shows what changed (reclaims, blocks, task creations)
- **Forward-looking**: "Next cycle priority" helps the next Director pass pick up without re-analyzing
- **Pattern-friendly**: Repeated hung workers, queue coverage gaps, and synthesis frequency become visible when scanning multiple entries

## Example (from Cycle #214)

```
=== DIRECTOR PASS — 2026-06-01T06:01:00+00:00 ===
Board: 17 running, 835 done, 0 ready
Workers: 21 active (worker-21 free)
Unsynthesized: 2 (exp_712, exp_733) — below ≥3 threshold, skipped synthesis
Queue: 8 active items, all covered by running experiments except [29]

Actions taken:
1. Reclaimed t_aac94959 (exp_734: hung — 0 CPU for 26m, workspace stale 18m)
2. Blocked t_aac94959 "hung: process alive but 0 CPU time for 26m"
3. Queue curation completed (59→30 items, 8 active remaining)

Queue coverage analysis:
  [22] Framing-dependent harm optimizer — COVERED (exp_717 running)
  [23] Source domain toxicity — COVERED (exp_724 running)
  [29] Augmentation paradox re-run — OPEN (no running coverage, exp_734 blocked)

Next cycle: Create task for [29] when saturation clears (need 2+ free workers)
```
