# Refactoring ledger

Tracked structural debt, in priority order. Items get removed when done —
this file shrinking is the metric. (Origin: an external static review,
2026-07; its two concrete bug finds — duplicated `normalize_domain` policy
drift and a stale 17-table `init_db()` bootstrap — were both confirmed and
fixed, which is why the rest of its list is taken seriously.)

## Done

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

## Open

### 1. Extract `auto_tune.py:main()` (~2,100 lines)
The four tuner phases have clean seams (search for the numbered comments):
`1.` outcome_bonus (~line 515), `2.` novelty (~617), `3.` injection rate
(~1360), `4.` trust_weight (~1830), plus the self-change guard (~2428).
Method: AST-derive each block's read/write variable sets, extract into
`_tune_<knob>(ctx, args)` functions sharing an explicit context object.
**Gate:** `--dry-run` output must be byte-identical before/after against the
same database snapshot (baseline capture procedure: run `--dry-run`, save,
refactor, re-run, diff). Do not extract without the gate.

### 2. Extract `apply_worker_results.py:apply_results()` (~1,340 lines)
No clean comment seams; this is the knowledge-ingest path (highest blast
radius in the system). Prerequisite: characterization tests that feed a
synthetic worker_results fixture through the full function against a
temp DB and assert the resulting rows. Write those first; extract second.

### 3. Finish path centralization
The migration covered module-level constants. Inline
`os.path.expanduser("~/.hermes/...")` call sites remain in many scripts —
migrate opportunistically when touching a file (`prometheus_paths.under_home`),
not as a big-bang rewrite.

### 4. Exception-handling triage
414 `except Exception` / 33 bare `except` across the tree. Policy (do not
blanket-remove — fail-open is deliberate in cron lanes):
- failure expected → log a structured reason
- failure invalidates evidence → mark the output unverified
- failure operational-only → fail open, but emit a heartbeat/status line
Apply the triage when touching a file; convert bare `except:` on sight.
