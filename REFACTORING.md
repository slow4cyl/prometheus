# Refactoring ledger

Tracked structural debt, in priority order. Items get removed when done —
this file shrinking is the metric. (Origin: an external static review,
2026-07; its two concrete bug finds — duplicated `normalize_domain` policy
drift and a stale 17-table `init_db()` bootstrap — were both confirmed and
fixed, which is why the rest of its list is taken seriously.)

## Done

- **Independence gate given teeth** — the armed-but-toothless gate now applies
  134 haircuts (was 0) and demoted exactly 4 over-trusted ESTABLISHED claims.
  Root cause: the durable prior_fed stamp postdated the shelf; the "unknown-body"
  premise was stale. `backfill_prior_feed_stamps.py` stamped 135,688 pre-stamp
  tasks (body-derived, reversible via `--rollback`), and
  `independence_gate.sweep_missing_stamps()` on the existing --check cron heals
  the ~24%/day enqueuer leak going forward. Verified: 4 demotions matched the
  simulation exactly (claims 66659/66878/69850/70091).
- **World gate armed** — `maturity.py` toy-vs-world gate: a claim whose latest
  verified world-grounding is FAILS cannot reach ESTABLISHED. Landed disarmed
  (proven byte-identical), then armed via `~/.hermes/world_gate_armed.json`
  (0 apply-day tier changes; binds prophylactically). Gate not weight; reversible
  by removing the arm file. 4 tests pin it.
- **CI** — `.github/workflows/tests.yml` runs the suite on every push (verified
  green in a clean minimal venv: pytest + PyYAML + numpy).
- **Config-drift + PR-watch sentinels** — on cron; the config sentinel guards
  the model-clobber class from this week's incident.
- **torn-extend WAL fix** — the obvious ≤1-page tolerance was adversarially
  disproven (see TORN_EXTEND_NOTES.md); the robust re-stat-and-persist form
  (re-stat backoff → healing WAL drain → double-drain stability proof) is now
  implemented and harness-validated (258,904 commits, 0 false alarms;
  positive control raises). Shipped in prometheus-fork `abd8a024b`;
  upstream-PR candidate.

- **Domain normalization single-source** — `apply_worker_results` delegates
  to `write_worker_result.normalize_domain`; regression-pinned by
  `tests/test_domain_normalization.py` (the banned lossy mappings can't
  silently return).
- **Schema single-source** — `prometheus_db.init_db()` now loads
  `schema/prometheus.schema.sql` and refuses to bootstrap a partial schema;
  the smoke test caught a real defect on day one (the dump carried SQLite's
  internal `sqlite_sequence`, making the shipped schema non-bootstrappable).
- **Central path policy** — `scripts/prometheus_paths.py`; 55 scripts'
  module-level path constants migrated. Inline one-off paths remain (see
  below).
- **Invariant tests** — `tests/` covers domain normalization, maturity
  policy, confidence clamp/calibration arithmetic, world-grounding basis
  classification, schema bootstrap, path policy. Run:
  `HERMES_HOME=$(mktemp -d) pytest tests/`.

- **`auto_tune.py:main()` extracted** — 2,094 → 177-line orchestration; the
  four numbered phases live in `_tune_outcome_bonus/_tune_novelty/
  _tune_injection_rate/_tune_trust_weight` plus `_self_change_guard`, sharing
  an explicit ctx (SimpleNamespace) threaded from AST-derived read/write sets;
  bodies moved byte-verbatim. Gate held: `--dry-run` byte-identical old-vs-new
  on the same DB snapshot (single wall-clock line scrubbed), plus a
  definite-assignment sweep showing zero unresolved free names on any path.
  Found along the way: auto_tune hardcodes `~/.hermes` via `$HOME` instead of
  honoring HERMES_HOME — a path-centralization item (see below).
- **`apply_worker_results.py:apply_results()` extracted** — 1,343 → 104-line
  orchestration; `_ApplyContext` + 25 stage helpers, bodies verbatim by
  construction (multiset line diff archived); the per-result `conn.commit()`
  stays visibly in the orchestrator loop (write discipline unchanged).
  Characterization EXTENDED first, against the monolith: 10 new golden-master
  tests pinning previously-uncovered stages (adversarial routing, arbitration
  seam, retest credit, boundary-lane closure, benchmark lineage — including
  pinning the dead `parent_benchmark_id` propagation AS dead — throttle +
  deep-lineage exemption, lineage_live, caveat confidence cap, PARTIALLY
  REFUTED/REFUTED_SETUP verdicts, junk-domain classification). Suite 56 green.

## Open

### 1. Finish path centralization
The migration covered module-level constants. Inline
`os.path.expanduser("~/.hermes/...")` call sites remain in many scripts —
migrate opportunistically when touching a file (`prometheus_paths.under_home`),
not as a big-bang rewrite. Known instance worth calling out: `auto_tune.py`
line ~32 derives its home from `$HOME`, not HERMES_HOME (its dry-run gate
harness has to fake `$HOME`).

### 2. Exception-handling triage
414 `except Exception` / 33 bare `except` across the tree. Policy (do not
blanket-remove — fail-open is deliberate in cron lanes):
- failure expected → log a structured reason
- failure invalidates evidence → mark the output unverified
- failure operational-only → fail open, but emit a heartbeat/status line
Apply the triage when touching a file; convert bare `except:` on sight.
