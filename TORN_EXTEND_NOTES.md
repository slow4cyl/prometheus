# torn-extend WAL invariant — why the obvious fix is wrong

> **RESOLVED 2026-07-09** — the robust form below is implemented in
> prometheus-fork commit `abd8a024b` (`hermes_cli/kanban_db.py`):
> re-stat backoff (~15ms, free when no deficit) → PASSIVE WAL drain
> (heals the killed-mid-checkpoint case instead of alarming) → **two
> bracketing full drains** with identical frame counters and an unmoved
> header (any concurrent writer appends frames; a checkpointer moves the
> header only by backfilling frames), and only then raise.
> Validated with the same harness shape that falsified the ≤1-page patch
> (`fork-patches/torn_extend_harness.py`): 4 writers × 25s, **258,904
> commits, 0 false alarms** (old patch: 79 in 20s), 0.09% of checks
> entered the re-stat path, one reached the drain (17.9ms max),
> integrity_check ok, positive control (real truncation, empty WAL)
> raises. Candidate for upstreaming as the reworked 0006 torn-extend
> hunk. The analysis below is kept as the record of why the constant
> tolerance was wrong.

`hermes_cli/kanban_db.py:_check_file_length_invariant` previously silently
returned in WAL mode. That was operationally safe but deleted a corruption
tripwire, so it was not upstreamable as-is.

## The obvious fix is WRONG (adversarially disproven 2026-07-09)

The intuitive patch — "tolerate a ≤1-page header/file mismatch in WAL mode,
raise beyond that" — was authored and then **falsified by LD_PRELOAD tracing
of a real PASSIVE checkpoint**:

- A PASSIVE checkpoint backfills pages in ascending order and writes **page 1
  (carrying the final page-count N+K) FIRST**, with the file-extending writes
  LAST. So a single commit that grows the DB by K pages opens a benign window
  with **deficit = K**, not 1.
- Multi-page growth is ordinary kanban traffic: one INSERT with a ~3.5KB body
  spills overflow pages → K ≥ 2.
- Live measurement: the ≤1-page patch **raised 79 times in 20s** of busy-board
  traffic on a database that was never corrupted (`PRAGMA integrity_check` ok
  throughout; every transient self-healed on the next checkpoint; raw deficits
  up to 4 pages observed).

So a fixed ≤1-page tolerance fixes only the K=1 sub-case while re-introducing
the exact false positive it targets — louder (raising, where the current code
silently returns). Net worse. **Not deployed, not submitted.**

## The required robust form (for a future dedicated pass)

Either:
1. **Re-stat and persist**: on WAL deficit > 0, re-stat after a short delay and
   raise only if the deficit PERSISTS. Real truncation persists; the checkpoint
   backfill race self-heals in microseconds. (Cost: a delay on the post-commit
   path — measure it.)
2. **Bound by checkpointed WAL growth**: tolerate a deficit no larger than the
   pages the in-flight checkpoint is known to be backfilling, rather than a
   constant.

Both need the race-reproduction harness the verifier built (LD_PRELOAD pwrite
trace + a busy-board load generator) to validate. This is a live-DB corruption
check — it gets its own focused session with that harness, not a batch item.

That dedicated pass happened 2026-07-09 (see the RESOLVED banner at the top):
form 1 shipped, hardened beyond the sketch with the healing drain + the
double-drain stability proof, and validated against the rebuilt busy-board
generator (`fork-patches/torn_extend_harness.py`). The old 0006-hunk4
silent-return is gone; the reworked check is the upstream-PR candidate.
