# Refactoring ledger

Tracked structural debt, in priority order. Items get removed when done —
this file shrinking is the metric. (Origin: an external static review,
2026-07; its two concrete bug finds — duplicated `normalize_domain` policy
drift and a stale 17-table `init_db()` bootstrap — were both confirmed and
fixed, which is why the rest of its list is taken seriously.)

## Done

- **Deployed-script drift sentinel** (2026-07-11 incident response) — an A1
  worker hit a routine argparse error calling `write_worker_result.py` and
  "fixed" it by rewriting the deployed script in place (10KB simplification:
  dropped `verify_artifacts`/`calibrate_confidence`, hardcoded calibration
  0.85/0.25, split `--files` on `;` instead of `,`, no task-context
  resolution). Every dependent cron failed on import for 2.5h; 456 rows
  written degraded (178 hardcoded calibration, all unverified, 174 missing
  task backlinks). Recovery: restored from repo, full calibration backfill,
  targeted re-verify + backlink reconstruction from archive manifests /
  dir-listings / workspace paths (83 VERIFIED, 359/456 backlinked; residual
  97 rows list files no archive holds). Prevention: `script_drift_sentinel.py`
  (repo↔live sha256, config_drift_sentinel conventions, 15m no-agent cron)
  + live `write_worker_result.py` chmod 555 (mv-based atomic deploys
  unaffected; worker `write_file` tool gets EACCES). Repo↔live reconciled:
  live `director.py` column fix (`kind` not `event_type`) and
  `inject_opportunities.py` fallback tune (15→8) synced into the repo.
  Lesson (extends the ORPHANED-WRITER class): a gated repo copy guarantees
  nothing about what is RUNNING — every deploy surface needs its own drift
  check.

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
  submitted upstream as NousResearch/hermes-agent#62122 (open).

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

- **Path centralization COMPLETE** — the remaining ~66 inline
  `expanduser("~/.hermes/…")` / `Path.home()` sites across 16 scripts
  (incl. the needs-care set: batch_create_tasks, curiosity_scorer,
  prometheus_db, independence_gate) migrated to `prometheus_paths`
  constants / `under_home()`. Gates held: value-equivalence proof (every
  migrated path resolves byte-identical on the live box), import smoke of
  all 16 under an isolated HERMES_HOME, suite green, and auto_tune's
  `--dry-run` byte-identical with the migrated curiosity_scorer in place
  (auto_tune rewrites that file as text). Deliberate exceptions kept and
  annotated: `write_worker_result`'s canonical-DB anchors (honoring
  HERMES_HOME there caused silent data loss), `experiment_rag._MAIN_HERMES`
  (an intentional main-home anchor), prompt-text literals, and
  `sys.path.insert` bootstrap lines. auto_tune itself honors HERMES_HOME
  (earlier commit).

## Open

### 1. Exception-handling triage
~477 `except Exception` across the tree (count grows with the codebase; it
is a policy surface, not a burn-down target). Bare `except:` is DONE in
scripts/ (all 24 converted to typed, with the liveness/ABANDON/commit paths
narrowed so a swallowed KeyboardInterrupt can't read as "worker dead" or
commit after an interrupt); 2 remain in `skills/kanban-worker/` reference
material. Policy for the `except Exception` sites (do not blanket-remove —
fail-open is deliberate in cron lanes):
- failure expected → log a structured reason
- failure invalidates evidence → mark the output unverified
- failure operational-only → fail open, but emit a heartbeat/status line
Apply the triage when touching a file; convert any new bare `except:` on
sight.
