# torn-extend WAL invariant — why the obvious fix is wrong

`hermes_cli/kanban_db.py:_check_file_length_invariant` currently silently
returns in WAL mode. That is operationally safe but deletes a corruption
tripwire, so it is not upstreamable as-is.

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

Meanwhile the live behavior (silent-return-in-WAL) stays; patch 0006-hunk4 is
NOT submitted upstream.
