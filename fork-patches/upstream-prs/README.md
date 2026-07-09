# Upstream PR prep — NousResearch/hermes-agent

Patch bundle extracted from the prometheus-fork stack for upstreaming.
0001/0002 staged 2026-07-04; 0003–0012 staged 2026-07-08 during the de-fork
migration (see docs/defork-plan.md and docs/architecture-changelog.md).

`gh` CLI is installed and authed on this box as of 2026-07-08 (account
slow4cyl; fork github.com/slow4cyl/hermes-agent) — submissions run directly
from here. Per-patch flow:

    git checkout upstream/main -B <branch> && git apply <patch>.diff
    git commit -am "<title>" && git push origin <branch>
    gh pr create --repo NousResearch/hermes-agent --head slow4cyl:<branch>
    # NOTE: branch names must not start with `test/` — upstream carries a
    # branch literally named `test`, so `test/*` refs are rejected.

## Inventory

| Patch | PR bundle | Diff base | Contents |
|---|---|---|---|
| 0001-scheduler-args | G: cron script args | merge-base 7e8f50a14 | shlex-split job `script` into path + argv (MIN_GRACE hunk excluded — site tuning, now lives in the prometheus-runtime-tuning plugin) |
| 0002-gc-grace | F: checkpoint gc | merge-base | gc `--prune=2.hours.ago` grace window |
| 0003-py314-daemon-pool-worker-sig | A: py3.14 compat | merge-base | daemon_pool `_worker` signature fix (py3.14 spawned 0 workers fleet-wide) |
| 0004-py314-status-setproctitle-argv | A: py3.14 compat | merge-base | gateway liveness check tolerates setproctitle-stripped argv |
| 0005-py314-env-loader-dotenv-retry | A: py3.14 compat | merge-base | dotenv reload KeyError retry (os.environ mutation race) |
| 0006-kanban-db-features | B: kanban_db robustness | **origin/main tip 449706cb5** | six features: init byte-lock serialization; A1 spawn routing (provider/model flags AFTER the `chat` token); bounded reaper retry; decompose children inherit root priority; TMPDIR=workspace temp routing; torn-extend WAL tolerance (submit the *tolerance* form per defork-plan.md — upstream will reject a silent-return that deletes a corruption tripwire) |
| 0007-kanban-tools-assignee-skills | C: kanban_tools UX | **origin/main tip** | assignee optional w/ coercion to 'default'; `skills` schema description tweak |
| 0008-overflow-handling | D: long-context overflow | merge-base | compression-attempts cap 3→30; output-cap reduction margin 64→512; overflow-spiral guard routing non-converging 400s into input compression; custom-profile max_tokens window clamp + context_length/estimated_input_tokens plumbing |
| 0009-goal-budget-cli | E: goal budget | merge-base | honor dispatcher-passed HERMES_KANBAN_GOAL_MAX_TURNS in the goal loop |
| 0010-checkpoint-gc-lock-loglevel | F: checkpoint gc | merge-base | concurrent-gc lock rejection logged at debug, not error. **Overlaps 0002** (same file, includes the grace hunk) — apply 0010 alone for the combined PR, or rebase-split |
| 0011-process-registry-venv-env | H: venv env injection | defork→prometheus-fork | `_inject_venv_env` for background process spawns. **DROPPED from base at the 2026-07-08 cutover** (defork reverted process_registry to stock); behavior returns only when this merges upstream |
| 0012-kanban-guidance-anti-false-block | I: prompt wording | defork→prometheus-fork | KANBAN_GUIDANCE tool-availability clarification (workers falsely blocked tasks claiming missing tools). **DROPPED from base at cutover**, same caveat as 0011 |
| 0013-test-config-set-real-home-write-leak | J: test hygiene (HIGH VALUE upstream — data-loss class) | git format-patch, applies to main | 5 `config.set` tests in `test_tui_gateway_server.py` write the REAL `~/.hermes/config.yaml` when the suite runs without `HERMES_HOME` (they mock `switch_model` but not `cli.save_config_value`; the path resolves from `cli._hermes_home` captured at import). Observed live: clobbered a production config to `anthropic/claude-sonnet-4.6` mid-suite. Fix mocks the persist in all five |

## Bases and rebasing

Patches marked *merge-base* were cut against fork point 7e8f50a14 and applied
cleanly there; expect small context drift against current main — rebase at
submission. 0006/0007 were cut against origin/main **tip** (449706cb5) because
the defork surgery rebuilt those files on tip; they apply clean today.
0011/0012 are cut defork→prometheus-fork (they re-add features the de-fork
dropped); regenerate context against main when submitting.

NOT staged deliberately: pyproject.toml `requires-python <3.15` raise
(deployment-local — upstream's <3.14 cap tracks their Rust transitive cp314
wheels, not a bug), and all prompt/config site tuning now carried by the
plugins (prometheus-guard, prometheus-prompt-policy, prometheus-runtime-tuning).

After a bundle merges upstream, delete its patch here and drop the
corresponding code at the next `hermes update`/rebase — the plugins and
sidecars are unaffected by base updates.

## Submission status — 2026-07-08 (submitted as slow4cyl via gh)

| PR | Branch | Covers |
|---|---|---|
| [#61224](https://github.com/NousResearch/hermes-agent/pull/61224) | fix/py314-compat | 0003+0004+0005 |
| [#61225](https://github.com/NousResearch/hermes-agent/pull/61225) | fix/tests-config-set-no-real-home-writes | 0013 |
| [#61226](https://github.com/NousResearch/hermes-agent/pull/61226) | fix/cron-script-args | 0001 |
| [#61227](https://github.com/NousResearch/hermes-agent/pull/61227) | fix/checkpoint-gc-grace | 0002+0010 |
| [#61228](https://github.com/NousResearch/hermes-agent/pull/61228) | fix/context-overflow-handling | 0008 |
| [#61229](https://github.com/NousResearch/hermes-agent/pull/61229) | fix/kanban-goal-max-turns-env | 0009 |
| [#61230](https://github.com/NousResearch/hermes-agent/pull/61230) | fix/kanban-create-assignee-coercion | 0007 |
| [#61231](https://github.com/NousResearch/hermes-agent/pull/61231) | fix/kanban-init-byte-lock | 0006 hunks 1-3 |
| [#61232](https://github.com/NousResearch/hermes-agent/pull/61232) | fix/kanban-decompose-child-priority | 0006 hunks 5-7 |
| [#61233](https://github.com/NousResearch/hermes-agent/pull/61233) | fix/kanban-reaper-bounded-retry | 0006 hunks 8-10 |
| [#61234](https://github.com/NousResearch/hermes-agent/pull/61234) | fix/kanban-worker-tmpdir-workspace | 0006 hunk 11 |
| [#61235](https://github.com/NousResearch/hermes-agent/pull/61235) | fix/kanban-model-override-arg-order | 0006 hunk 12 |
| [#61221](https://github.com/NousResearch/hermes-agent/pull/61221) | fix/process-registry-venv-env | 0011 |
| [#61222](https://github.com/NousResearch/hermes-agent/pull/61222) | fix/kanban-guidance-tool-availability | 0012 |

NOT submitted: 0006 hunk 4 (torn-extend WAL tolerance) — must be reworked into
the bounded-tolerance form before upstreaming (the fork's silent-return would be
rejected as deleting a corruption tripwire); it remains in our base meanwhile.
Fork lives at github.com/slow4cyl/hermes-agent; gh CLI is now installed and
authed on this box, so future submissions can be driven directly.
As each PR merges: delete its patch file here and drop the matching base
commit(s) at the next rebase.
