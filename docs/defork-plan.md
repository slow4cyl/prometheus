# De-forking Prometheus from hermes-agent — feasibility & migration plan

**Date:** 2026-07-08 · **Verdict: feasible — ~95% of the fork can leave the repo.** Produced by a 64-agent analysis (extension-surface inventory → per-mod diff analysis → per-feature routing → adversarial verification against the actual code). 39 features across the 18-file patch stack: 32 routes CONFIRMED by verification, 4 corrected, 3 unverified (verifier crashes — re-check before relying on them).

## Bottom line

The fork is not one problem but four, and each has an exit that Hermes already supports today:

| Route | Features | What it means |
|---|---|---|
| `upstream_pr` | 26 | Generic fixes/bugs upstream should take. 2 already staged in `~/.hermes/fork-patches/upstream-prs/` (0001 scheduler-args, 0002 gc-grace); ~5 more PRs to stage. Includes 3 "collateral revert" hunks where the fork accidentally reverted NEWER upstream work (lifecycle plugin hooks, claim-lock reclaim guards, dispatch-tick lock) — those exit by simply re-adopting upstream at the next rebase. |
| `sidecar` | 4 | External cron/service stamping kanban.db columns the upstream dispatcher ALREADY reads (the a1_router pattern). Notably: the fleet-wide goal loop can use upstream's own per-task `goal_mode`/`goal_max_turns` columns — no code mod needed at all. |
| `runtime_patch_plugin` | 4 | A `~/.hermes/plugins/` plugin whose import-time code monkeypatches a base symbol, with a sentinel guard (exact base-phrase check → fail LOUD on upstream drift). Verified viable: `discover_plugins()` runs in every agent process before first prompt build. For: memory-guardrail prompts ×2, MIN_GRACE, redaction removal. |
| `plugin_hook` | 3 | Documented `pre_tool_call` hook. 2 of 3 ALREADY live in `prometheus-guard` (the in-file kanban_tools copies are pure dead weight — delete at next rebase); the third (worker-result-written completion gate) is a small extension of the same plugin. |
| `config_or_env` | 2 | Existing knobs: `kanban.auto_subscribe_on_create: false`, `skills.external_dirs: [~/.hermes/skills]`. (A third instance — venv PATH — was ALREADY migrated via the gateway systemd unit on 2026-07-04; the process_registry fork hunk is partly redundant today.) |
| `fork_only` | 1 | The `kanban_create` skills schema-description tweak — cosmetic, documents fork-specific behavior; drop it when the skill-injection feature migrates. |

## Honest caveats

- **"Upstream PR" is not "gone"** — until a PR merges, the hunk stays in the fork. But these are small, isolated, loudly-conflicting hunks (cheap rebases), and if upstream rejects one it usually degrades to a knob-request or a guarded plugin, not a dead end. No `gh` on this box: PRs must be submitted manually (see `fork-patches/upstream-prs/README.md`).
- **Runtime patches trade rebase pain for drift risk.** Every monkeypatch MUST carry a sentinel (assert the base constant/function matches a known fingerprint; on mismatch write a PATCH_FAILED marker + log loudly, and fail OPEN so worker sessions never die from a guard bug). Add a cron tripwire on the marker file.
- **Sidecars are best-effort per-row** (INSERT→claim race ≈ one 10s dispatcher tick) — verified acceptable for goal-mode/skills stamping (reaper retry + janitor heal misses), NOT acceptable where the fork guarantees per-spawn behavior (that's why A1 arg-order stays a PR, not a sidecar). A silently-dead sidecar reverts its whole policy — wire each into the cron freshness watchdog.
- **3 features are UNVERIFIED** (verifier agents died): overflow-spiral guard PR shape, TMPDIR=workspace routing, dispatch-tick-lock revert. Re-verify before acting on those three.

## Suggested phasing (each phase independently shippable)

1. **Free wins, no upstream needed (removes the 2 biggest policy diffs):** delete in-file kanban_tools guards (plugin already covers); add completion-gate branch to prometheus-guard; set the 2 config knobs; build the prompt-policy runtime-patch plugin (memory guardrails) + cron-grace wrapper plugin; stand up the goal-mode/skills/assignee-rebalance stamper sidecar (one script, a1_router pattern); move provenance-pin logic into write_worker_result.py.
2. **Rebase cleanup:** re-adopt the 3 collaterally-reverted upstream features (upstream's lifecycle plugin hooks are themselves NEW extension surface); drop the cosmetic schema tweak.
3. **Upstream campaign:** stage + submit the ~7 PR bundle (py3.14 ×3, A1 clamp+plumbing, conversation_loop bundle, scheduler-args [staged], gc-grace [staged], A1 arg-order + model_override generalization, decompose priority, reaper retry, assignee coercion, byte-lock knob-ification, goal-budget env reader).
4. **End state:** fork = only unmerged PR hunks. `hermes update` = fetch + rebase with rare, loud, small conflicts; plugins/sidecars/config survive untouched by construction.

---

# Per-feature routing table (verified)


### Route: upstream_pr

**a1_clamp — context_length + estimated_input_tokens plumbing into build_kwargs**  
Status: CONFIRMED  
How: Ships in the SAME staged PR as the clamp — the 6-line kwargs addition at agent/chat_completion_helpers.py:828-833 (context_length from agent.context_compressor.context_length via None-safe getattr chain; estimated_input_tokens from the pre-existing estimate_request_context_tokens over the exact {messages, tools} payload) is the clamp's only data feed and is inert for non-custom profiles (unknown params ignored in _build_kwargs_from_profile's params dict, read at chat_completions.py:530-531). It has no standalone value, so it must not be routed separately: if the interim llm_request-middleware   
Risk: Coupled to the clamp's upstream fate — if upstream restructures the fix (e.g. computes the estimate inside the transport or fixes the custom plugin default instead), this hunk disappears or moves, and carrying it alone in the fork is pure rebase cost with zero behavior. Also inherits the chars/4 und

**a1_clamp — custom-profile max_tokens window clamp**  
Status: CONFIRMED  
How: Stage as fork-patches/upstream-prs/0003-custom-profile-max-tokens-clamp.diff (existing convention: 0001/0002 + PR-*.md already there): the ~30-line block at agent/transports/chat_completions.py:517-549 plus its test. It fixes a real upstream bug — the custom provider plugin's default_max_tokens=65536 equals the whole window, which strict OpenAI-compatible servers (vLLM) reject with HTTP 400 whenever input+output exceeds context, triggering the retry+compression spiral; the in-code comment (chat_completions.py:523-528) is already written as an upstream rationale. Nothing Prometheus-specific: co  
Risk: Upstream may prefer fixing the custom plugin's default_max_tokens (plugins/model-providers/custom/__init__.py:91) instead of a transport clamp, or bikeshed the 4% reserve — until merged the fork carries the edit through every rebase of chat_completions.py (an active file). The middleware fallback ha

**checkpoint_manager — concurrent-gc lock rejection logged at debug instead of error**  
Status: CONFIRMED  
How: Stage as its own patch (e.g. 0003-gc-lock-debug.diff) or fold into PR-0002 (same file, same concurrency theme; currently NOT in the staged diff — grep for the substring returns 0). The change is the single failure branch in _run_git, tools/checkpoint_manager.py:343-354 (commit ca5da54ef): if stderr contains 'gc is already running', logger.debug instead of logger.error; return values unchanged. Generic: git's gc.pid lock rejection is an expected benign race for any two concurrent sessions, and ERROR spam buries real failures everywhere. Runtime_patch_plugin was evaluated and rejected: the gatew  
Risk: Matches git's English porcelain text — a localized git (unusual; _git_env does not force LC_ALL) falls through to error, which is only noise regression, not silent breakage. Upstream refactor of the _run_git failure branch drops the hunk with a visible rebase conflict (loud). If upstream rejects the

**checkpoint_manager — grace-window gc prune (--prune=2.hours.ago instead of --prune=now)**  
Status: CONFIRMED  
How: Already staged: ~/.hermes/fork-patches/upstream-prs/0002-gc-grace.diff + PR-0002.md cover all four gc call sites (tools/checkpoint_manager.py:1127, 1220, 1437, 1514) with the exact fork hunks. Generic fix: any deployment with >1 concurrent session shares the single bare store at ~/.hermes/checkpoints (_store_path, checkpoint_manager.py:207), and the gateway spawns multiple workers by default, so --prune=now is unsafe upstream too; 2h grace is a sane default (git's own default is 2 weeks). Before submitting, fix the misindented comment lines inside the arg list at the ~1506-1514 hunk (present i  
Risk: Until merged, any upstream edit to the four gc call sites conflicts on rebase (4 separate hunks in one file). If upstream later adds a fifth gc invocation against the shared store, the fork must remember to patch it — a missed site reintroduces the corruption silently (symptom is only rc=128 gc abor

**conversation_loop — Compression-attempts cap raised 3 -> 30**  
Status: CONFIRMED  
How: Upstream will not take a hardcoded 30 (interactive users want the fast-fail), so the PR is knob-ification: introduce `compression.max_attempts` (default 3, preserving upstream behavior) read once in run_conversation and used at BOTH enforcement sites — the preflight gate literal at agent/conversation_loop.py:1006 (and its ':1014 attempt=%s/30' log string) and max_compression_attempts at :1088 (consumed at :3102, :3328, :3457, :3572). After merge the fork's 30 collapses to one line in ~/.hermes/config.yaml and ~/.hermes/profiles/a1/config.yaml (per-profile config is a full HERMES_HOME override   
Risk: Highest rejection risk of the three — it's a policy knob, not a bug fix; upstream may argue 30 compressions means the session is pathological. If declined it stays fork_only (two-literal edit, trivial to carry but both sites at :1006 and :1088 must stay in lockstep or the preflight and error-driven 

**conversation_loop — Larger safety margin on single output-cap reduction (64 -> 512 tokens)**  
Status: CONFIRMED  
How: Fold into the same 0003 upstream PR as the spiral guard — it is one line in the same elif branch (safe_out = max(1, available_out - 512) at agent/conversation_loop.py:3447) and the justification is shared: the wider margin absorbs input re-estimation drift between retries so a near-miss does not consume the second strike that arms the spiral guard. Cost argument for reviewers: at most 448 output tokens on one already-degraded retry. Fallback if upstream balks at the constant: a guarded runtime_patch_plugin CAN express this one (unlike feature 1) — patch agent.conversation_loop.parse_available_  
Risk: As a bundled PR: none beyond feature 1's rebase surface. The runtime-patch fallback is the fragile part: it silently also shifts the number the spiral-guard/fail-fast logic sees, and if upstream ever changes the -64 to something else the fingerprint guard must fire or you get a double margin.

**conversation_loop — Overflow-spiral guard: route non-converging output-cap 400s into input compression**  
Status: UNVERIFIED (verifier died)  
How: Stage as fork-patches/upstream-prs/0003 following the existing README.md convention (git-apply-clean diff vs origin/main + PR-0003.md body). The patch is exactly the fork hunks: output_cap_reductions counter at agent/conversation_loop.py:610, increment at :3427, the >=2 branch at :3428-3442 that resets agent._ephemeral_max_output_tokens and falls through to the input-compression path at :3521+, and the `and output_cap_reductions < 2` relaxation of the #55546 fail-fast at :3488. Pitch: any strict OpenAI-compatible endpoint (vLLM, llama-server) reports a DERIVED bound (context_length+1-max_token  
Risk: Until merged it is rebase surface: run_conversation is being actively decomposed upstream (module docstring says it was already extracted from AIAgent), so the :3403-3521 branch may move and the diff stops applying — loud failure at rebase, acceptable. If upstream instead fixes it differently (e.g. 

**cron_scheduler — Script-field argument passing (shlex split of job 'script' into path + argv)**  
Status: CONFIRMED  
How: Already staged: ~/.hermes/fork-patches/upstream-prs/0001-scheduler-args.diff + PR-0001.md, extracted from fork commit e591d67bc (minus the MIN_GRACE hunk) and verified to `git apply --check` cleanly on origin/main 7e8f50a14 per the README. The diff is exactly the fork change: `parts = shlex.split(script_path)` with the path-traversal guard applied to parts[0] (cron/scheduler.py:1892-1896) and `extra_args` appended to both the bash argv (:1939) and python argv (:1941); backward compatible (bare paths split to themselves, spaced filenames quote). Submit from a machine with gh/push access per REA  
Risk: Until merged the fork must carry the commit through every rebase (it touches a single function, low conflict surface). If upstream rejects or reshapes it (e.g. adds a separate `args` job field instead of splitting `script`), the 18 live jobs.json entries need a one-time format migration — a mechanic

**goal_budget_cli — Honor dispatcher-passed HERMES_KANBAN_GOAL_MAX_TURNS env budget in the goal loop**  
Status: CONFIRMED  
How: Submit the 2-line consumer fix as a staged patch in ~/.hermes/fork-patches/upstream-prs/ (alongside 0001-scheduler-args.diff / 0002-gc-grace.diff): in _run_kanban_goal_loop_q (fork cli.py:15530-15531, upstream cli.py ~15618) replace `max_turns = task.goal_max_turns or _DEF_TURNS` with the three-tier `task.goal_max_turns or (int(env) if env.isdigit() and int(env) > 0 else 0) or _DEF_TURNS`. Pitch: upstream already writes HERMES_KANBAN_GOAL_MAX_TURNS at spawn (origin/main kanban_db.py:7739) but no code ever reads it — this is a protocol completion making the spawn env an effective budget channel  
Risk: Two failure modes: (1) upstream may resolve the dead env write the other way — delete kanban_db.py:7739 instead of adding a reader — which kills the channel entirely; fallback is a sidecar that stamps tasks.goal_max_turns (upstream column, kanban_db.py:1135) on ready rows via conditional UPDATE ... 

**kanban_db — A1 spawn routing — provider/model flags AFTER the chat token**  
Status: CONFIRMED  
How: PR the placement fix: emit model_override as chat-subcommand options (cmd.extend(["chat", *_model_override_args, ...]), fork kanban_db.py:7738-7739) because argparse subparser defaults clobber top-level -m/--provider — this silently breaks ANY upstream user of tasks.model_override. Generalize the hardcoded 'agents-a1' special case (fork :7727-7731) by resolving model_override against providers.*/custom_providers default_model + model_aliases to derive --provider custom:<name>, closing the inventory's stated near-miss ('does not generalize to other custom providers'). Stage as 0003 in ~/.hermes  
Risk: Until merged, every rebase must re-carry the splice; if upstream restructures _default_spawn argv assembly the fork patch conflicts loudly (good) but an upstream partial fix (e.g. only the -m placement, not provider mapping) would leave A1 tasks silently running on the config-default provider again 

**kanban_db — Blocking sidecar byte-lock init serialization (~20 writers)**  
Status: CONFIRMED  
How: PR: make _cross_process_init_lock's timeout configurable (e.g. HERMES_KANBAN_INIT_LOCK_TIMEOUT_SECONDS or kanban.init_lock_timeout_seconds; 0 = block forever), keeping upstream's 10s default — the fork then expresses its policy as config_or_env. Also propose keeping the _INITIALIZED_PATHS fast path but gating it behind the same knob. No other route reaches this: it runs inside connect() (fork kanban_db.py:1299/~1554) in EVERY process including bare `hermes kanban` CLI invocations that never load plugins, so runtime_patch_plugin has a coverage hole, and a sidecar cannot serialize in-process ope  
Risk: Upstream added the bounded 10s escape hatch deliberately (#36644/#50353 hang-avoidance) and may reject re-introducing an unbounded mode; if rejected this is effectively fork_only, and every rebase silently restores 'proceed without lock after 10s', re-exposing 20-writer concurrent header-validation/

**kanban_db — Bounded reaper retry for protocol violations**  
Status: CONFIRMED  
How: PR the tiering in detect_crashed_workers (fork kanban_db.py:6334-6341): systemic same-fingerprint crashes keep _flim=1, clean-exit-without-lifecycle-call gets a configurable kanban.protocol_violation_failure_limit (default 3), citing the 741/775=96% later-run completion evidence from commit 001253446 — a generic over-aggression fix for any kanban deployment. Interim bridge already deployed: task_janitor_v2.py's unblock/reclaim + abandon-after-MAX_BLOCKED_EVENTS=3 (:199-243) approximates the same retry budget post-hoc.  
Risk: Upstream may defend failure_limit=1 as 'deterministic failure stays failed'; if rejected, the janitor fallback works but each retry cycle costs up to 10 min of cron latency plus a wasted auto-block/unblock event pair per attempt, inflating the gave_up/blocked telemetry the deployment analyzes.

**kanban_db — Decompose children inherit root task priority**  
Status: CONFIRMED  
How: PR decompose_triage_task reading root priority in the root-row SELECT and binding it into each child INSERT (fork kanban_db.py:4886-4935) — inheritance is the correct default anywhere dispatch ordering exists; upstream's column-default p0 children starve under any priority-ordered refill, not just Prometheus's. The already-running sidecar bridge is task_janitor_v2.py:362-381 (decompose-child priority inheritance via from_decompose_of task_events), whose own comment calls it a stopgap 'until the source fix lands'.  
Risk: Minimal — smallest, most obviously-correct patch in the set; only exposure is the interim window where the janitor's 10-min age guard leaves fresh child families parked at p0 before repair.

**kanban_db — REMOVED: board-scoped dispatch tick single-writer lock (collateral revert of upstream #35240 defense)**  
Status: UNVERIFIED (verifier died)  
How: Un-revert: restore the _dispatch_tick_lock non-blocking .dispatch.lock wrapper and DispatchResult.skipped_locked around dispatch_once (fork :6789) from merge-base at next rebase. Note the exposure is already live, not hypothetical: `hermes kanban dispatch` from the CLI (hermes_cli/kanban.py) shares no guard with the gateway's embedded tick (gateway/kanban_watchers.py → dispatch_once), so any manual/cron dispatch races the gateway's reclaim/spawn phase today — and every sidecar route recommended above assumes exactly-one-dispatcher semantics.  
Risk: None to restoring (single systemd gateway means the lock is a no-op in the happy path); leaving it out means one stray `hermes kanban dispatch` — or a future second dispatcher-capable process — silently races reclaim sweeps and WAL frames with no skipped_locked signal to detect it.

**kanban_db — REMOVED: claim-lock-aware reclaim guards (collateral revert of upstream #50366)**  
Status: CONFIRMED  
How: Un-revert: restore the compare-and-swap conditions (AND worker_pid=? AND claim_lock IS ?) to the three reclaim UPDATEs in enforce_max_runtime (fork :5967), detect_stale_running (:6092), detect_crashed_workers (:6272) by taking merge-base code at next rebase — pure patch-capture collateral from cf3938bbf with no Prometheus policy content; composes cleanly with feature 5's failure-limit tiering, which touches the failure-recording path, not the reclaim WHERE clause.  
Risk: Leaving it reverted is the risk: with ~20 workers and frequent reaper-driven respawns, an unguarded reclaim can silently reset a freshly re-claimed running task to ready (the task/run desync #50366 fixed), corrupting run accounting that Prometheus telemetry (gave_up counts, worker_results joins) dep

**kanban_db — REMOVED: kanban lifecycle plugin hooks (collateral revert of upstream #50349)**  
Status: CONFIRMED  
How: Un-revert — upstream already has the feature at the merge-base (def _fire_kanban_lifecycle_hook at 7e8f50a:kanban_db.py:140, fire sites at 3484/4162/4633/4745); drop the collateral revert at next rebase, nothing fork-side to preserve. URGENT independent of routing: verification found the fork kept TWO blocked-path call sites (fork kanban_db.py:4439 and 4551) while deleting the definition — repo-wide grep finds no def and no import — so block_task's hook path raises NameError on this checkout; restoring the upstream definition fixes a live crash AND revives the observer surface prometheus-guard  
Risk: None to restoring it (upstream's direction); the risk is inaction — a latent NameError in the blocked transition plus a permanently dead kanban_task_claimed/completed plugin surface that future sidecar-retirement plans (like the e6004d504→plugin migration) would depend on.

**kanban_db — TMPDIR=workspace temp routing (replaces upstream TERMINAL_CWD pin)**  
Status: UNVERIFIED (verifier died)  
How: PR adding env['TMPDIR']=workspace ALONGSIDE upstream's existing TERMINAL_CWD=workspace pin (merge-base 7e8f50a kanban_db.py:7713-7726) — per-task scratch dying with the workspace is generic hygiene (motivating data: 24GB tmpfs at 100%, 82k gpu_sklearn_* dirs), and the two pins are explicitly not in conflict; the fork's TERMINAL_CWD loss was collateral patch-capture (cf3938bbf), so the PR simultaneously heals that regression (#50348 re: #41312/#34619). No other route reaches spawn env construction: no pre-spawn plugin hook exists (VALID_HOOKS plugins.py:135-215 has none), sidecars provably cann  
Risk: Upstream may worry about workloads assuming tmpfs-speed /tmp or about TMPDIR leaking into nested tools; if rejected, the only guarded alternative is a runtime_patch_plugin wrapping _default_spawn in the gateway process (plugins load before the dispatcher watcher starts, gateway/run.py:7130), which i

**kanban_db — Torn-extend invariant downgraded to silent return in WAL mode**  
Status: CORRECTED → upstream_pr — but the PR must be race-free, not magnitude-bounded: either (a) tolerate ANY header/file deficit while journal_mode=WAL and re-verify only after a COMPLETED wal_checkpoint (raise solely   
How: PR the WAL-awareness upstream as a bounded tolerance rather than the fork's silent return: in _check_file_length_invariant (fork kanban_db.py:2078-2090), tolerate a ≤1-page header/file mismatch when journal_mode=WAL (or retry once after a passive checkpoint), keep raising beyond that, and keep the untouched non-WAL backup-path check (~:2164). The false positive (checkpoint updates header page_count before the file extends) hits ANY multi-writer WAL deployment of upstream — cite the deployment's architecture-changelog 'Fix 2' (2026-06-17) evidence. Same every-process coverage argument as the in  
Risk: Upstream will likely reject the fork's version verbatim (deleting a corruption tripwire) — the PR must be the tolerance form or it stalls; until merged, each rebase resurrects spurious sqlite3.DatabaseError on busy WAL boards (at least it fails loud, so regressions are visible immediately).

**kanban_tools — kanban_create 'skills' schema description tweak**  
Status: CORRECTED → fork_only  
How: One-line doc PR: change the skills parameter description in KANBAN_CREATE_SCHEMA from 'The kanban lifecycle is already injected automatically' to reference the built-in kanban-worker skill (fork tools/kanban_tools.py:1933-1936) — upstream does force-load kanban-worker at spawn (kanban_db.py:7704), so the correction is factually right for upstream too and trivially mergeable. If it stalls, a skill-level restatement in the kanban-worker/orchestrator skills covers the same model-facing guidance at zero fork cost.  
Risk: Essentially none — doc-only, no code path; worst case the PR is ignored and the fork either carries a 4-line diff or drops it (the behavioral cost of the stale base prose is negligible).

**kanban_tools — kanban_create assignee made optional with coercion to 'default'**  
Status: CONFIRMED  
How: Generic upstream PR (stage in ~/.hermes/fork-patches/upstream-prs/ alongside 0001/0002, which do not cover this): in _handle_create, validate assignee against hermes_cli.profiles.profile_exists and fall back to a configurable kanban.fallback_assignee (or reuse the existing kanban.default_assignee key, whose dispatcher-side precedent at kanban_db.py:6915-6973 makes acceptance likely), dropping 'assignee' from the schema's required list — hallucinated/stale profile names orphaning ready tasks is a failure mode for ANY multi-profile deployment, not fleet policy. Cannot be a plugin_hook: pre_tool_  
Risk: Until merged, the fork carries the diff across rebases (tools/kanban_tools.py:1252-1265 sits in a high-churn handler); upstream may insist on erroring rather than coercing (their comment says the requirement is deliberate), in which case the durable fallback is the janitor sidecar + a pre_tool_call 

**process_registry — venv env injection for background process spawns (_inject_venv_env)**  
Status: CORRECTED → Split verdict. PATH half: config_only and ALREADY SHED — the live base unit ~/.config/systemd/user/hermes-gateway.service sets Environment="PATH=~/.hermes/hermes-agent/venv/bin:...  
How: Stage fork-patches/upstream-prs/0003 (convention already live: 0001-scheduler-args.diff, 0002-gc-grace.diff exist) carrying the sys.prefix-keyed PATH prepend into both spawn_local branches (tools/process_registry.py:749 PTY, :792 Popen). Reshape before submitting: keep ONLY the PATH prepend ('if sys.prefix != normpath(sys.base_prefix): prepend <sys.prefix>/bin to env PATH') and DROP the env['VIRTUAL_ENV']=sys.prefix line — python3 resolution needs only PATH, and upstream strips VIRTUAL_ENV deliberately (tools/environments/local.py:240 _ACTIVE_VENV_MARKER_VARS, #23473 uv/poetry cross-project-cl  
Risk: Three failure modes: (1) upstream rejects even the PATH-only PR (they may prefer fixing it in the launcher/docs), leaving the fork carrying fdfb361f2 across rebases — rebase surface is small (1 helper + 2 call sites) but spawn_local is actively evolving (Windows winpty branch recently added), so con

**prompts — KANBAN_GUIDANCE tool-availability clarification (anti-false-block)**  
Status: CONFIRMED  
How: Stage as PR-0003 in ~/.hermes/fork-patches/upstream-prs/ (mirrors existing 0001-scheduler-args.diff / 0002-gc-grace.diff + PR-000N.md convention). The change is a factually-true, deployment-agnostic clarification of agent/prompt_builder.py:186-193 — kanban workers in ANY hermes deployment have the full standard toolset alongside kanban_*, and false-blocking on imagined tool absence is a model-agnostic failure mode; zero Prometheus policy content, 5-line diff, easy review. Until merge, the fork commit carries it; if faster interim coverage off-fork is wanted, the same prompt-policy plugin from   
Risk: Upstream may reword rather than merge verbatim, leaving the fork hunk in conflict at next rebase; until merged the fork carries a hot hunk inside KANBAN_GUIDANCE, a block upstream actively edits (835-token lifecycle text), so rebase-conflict probability is moderate. If routed through the interim run

**py314_bugfixes — daemon_pool py3.14 _worker signature compatibility**  
Status: CONFIRMED  
How: Extract tools/daemon_pool.py:52-67 (the hasattr(self,'_create_worker_context') feature-detect choosing the 3.14 (executor_ref, ctx, work_queue) tuple vs the 3.8-3.13 4-tuple) as a standalone diff staged in ~/.hermes/fork-patches/upstream-prs/ following the existing 0001/0002 pattern (diff + PR-000N.md body, verified with git apply --check on origin/main). Pitch: hard crash — every DaemonThreadPoolExecutor worker thread dies on TypeError under py3.14, so the pool silently executes nothing in cli.py, agent/tool_executor.py, gateway dispatcher, delegate/skills_hub/memory paths. Suggest upstream a  
Risk: Depends on CPython private API concurrent.futures.thread._worker; py3.15 could change the signature again and the hasattr probe on _create_worker_context could pass while the tuple shape drifts — failure mode is again silent (threads die, pool does nothing) unless upstream adds the loud signature gu

**py314_bugfixes — dotenv reload KeyError retry (os.environ mutation race)**  
Status: CONFIRMED  
How: Extract hermes_cli/env_loader.py:146-164 (3-attempt for-loop catching KeyError around load_dotenv, re-raising on attempt 3, preserving the UnicodeDecodeError→latin-1 fallback) into the ~/.hermes/fork-patches/upstream-prs/ staging series. Pitch: base-vs-base race — gateway per-turn reload (gateway/run.py:1275-1297, called at :1334) vs kanban_watchers.py:1160-1208 pinning/unpinning HERMES_KANBAN_BOARD in os.environ; python-dotenv resolve_variables does env.update(os.environ) which can KeyError mid-iteration; the failed load applied nothing so retry is idempotent. Trim the '#external-probe' site-  
Risk: Retry masks rather than removes the race — if upstream instead serializes os.environ mutation (e.g. a lock in kanban_watchers) the patch becomes dead code (harmless). Real residual: a KeyError from a genuinely broken .env would be retried twice then raised, slightly delaying a legit failure. Rebase 

**py314_bugfixes — gateway liveness check tolerates setproctitle-stripped argv**  
Status: CONFIRMED  
How: Extract gateway/status.py:385-403 (strict-argv branch now gated on _gateway_command_subcommand(live_cmdline) is not None; stripped/bare-'hermes' proctitle falls through to _record_looks_like_gateway(record), safe because the caller already verified pid+start_time at status.py:905-914) into the upstream-prs staging series. Pitch: base main.py:82-86 installs setproctitle('hermes') whenever the optional package is present, erasing the 'gateway run' subcommand from /proc cmdline, so any Linux user with setproctitle gets a healthy systemd gateway misreported OFFLINE by `hermes gateway status`, rest  
Risk: Correctness hinges on the caller contract (pid liveness + start_time match checked at status.py:905-914 BEFORE this function) — an upstream refactor that calls _record_matches_live_gateway_pid without that pre-check reopens a PID-reuse false-ONLINE window, and nothing fails loud; the PR body should 

**skills_tool — Lazy call-time resolution of the skills directory (_get_skills_dir)**  
Status: CONFIRMED  
How: Already landed upstream in equivalent-and-better form: origin/main commit f8723c478 adds _skills_dir() guarded by the _SKILLS_DIR_AT_IMPORT sentinel (tools/skills_tool.py:94-109 upstream) — call-time get_hermes_home()/'skills' unless SKILLS_DIR was monkeypatched — and converts all five call sites the fork converted (upstream :522 _get_category_from_path, :641 _find_all_skills, :721 skills_list, :1005 skill_view active_skills_dir which feeds trusted-dirs :1157 and rel_path :1397). On the next rebase, drop the fork's _get_skills_dir (fork tools/skills_tool.py:96-104) in favor of upstream's helpe  
Risk: Near zero post-rebase since upstream owns the fix. Until rebase: the fork helper is named _get_skills_dir vs upstream's _skills_dir, so a careless merge could keep both and leave fork-local callers on the non-sentinel version (losing test-monkeypatch respect). The skill_commands.py residue means the


### Route: config_or_env

**kanban_tools — Auto-subscribe removed from kanban_create**  
Status: CONFIRMED  
How: Exact existing knob: base gates the call behind cfg_get(cfg,'kanban','auto_subscribe_on_create',default=True) (origin/main tools/kanban_tools.py:993; docstring at :962; same gate visible in the fork's dead copy at :1408). Set `kanban: {auto_subscribe_on_create: false}` in ~/.hermes/config.yaml AND ~/.hermes/profiles/*/config.yaml (per-HERMES_HOME divergence gotcha from the surface inventory) — identical end state (no kanban_notify_subs rows) with zero code. This also removes the fork's incomplete-removal debris (dead _maybe_auto_subscribe with latent NameError on load_config at fork :1407/1461  
Risk: Config is read per-HERMES_HOME at process start: a new profile created without the key silently reverts to auto-subscribe (default True); the base response will include 'subscribed: false' which the fork's trimmed _ok omits — cosmetic only, no consumer depends on it. Requires the base code path, i.e

**skills_tool — Hard-coded ~/.hermes/skills fallback search dir in skill_view (_get_all_skill_dirs)**  
Status: CONFIRMED  
How: Replace the code fallback with the existing skills.external_dirs knob, set per profile: change ~/.hermes/profiles/a1/config.yaml:373 and ~/.hermes/profiles/interactive/config.yaml:371 from 'external_dirs: []' to 'external_dirs: [~/.hermes/skills]'. Verified mechanics: skill_view appends get_external_skills_dirs() to its search list (fork tools/skills_tool.py:1016-1018; upstream :1008) and marks them trusted (fork :1164-1170, upstream :1159); get_external_skills_dirs reads the CALL-TIME config — get_config_path() = get_hermes_home()/'config.yaml' (hermes_cli/config.py:675-677) — so inside the d  
Risk: Per-profile config drift: a newly created profile without the external_dirs entry silently loses the fallback (mitigate with the profile template or a config-audit cron in the task_janitor class). Silent-failure mode if upstream ever changes get_external_skills_dirs' skip test from call-time local_s


### Route: plugin_hook

**kanban_tools — Tool-hallucination guard on kanban_block**  
Status: CONFIRMED  
How: ALREADY DONE — pre_tool_call hook in ~/.hermes/plugins/prometheus-guard/__init__.py:370-410 engages on tool_name=='kanban_block', runs the same _check_tool_hallucination pattern list, and returns {'action':'block','message':'BLOCK REJECTED...'}; enforced at agent/tool_executor.py:418-432 via get_pre_tool_call_block_message (hermes_cli/plugins.py:2049) BEFORE the handler runs, producing the identical {'error': msg} shape as the fork's tool_error(). The in-file copy at tools/kanban_tools.py:1072-1074 can be deleted from the fork; plugin.yaml records it already retired fork commit e6004d504. 128   
Risk: Plugin loads per-process: fresh worker spawns pick it up, but if user-plugin discovery (plugins.py:1305) is ever disabled or the plugin dir moves, the guard vanishes silently (fail-open by design, __init__.py:357-364); an upstream rename of the pre_tool_call hook or the tool_executor call site would

**kanban_tools — Uncertainty-deferral guard on kanban_block**  
Status: CONFIRMED  
How: ALREADY DONE — same prometheus-guard _pre_tool_call handler calls _check_uncertainty_deferral (plugin __init__.py:388-395) after the hallucination check, reading ~/.hermes/prometheus.db worker_results and the kanban task body via mode=ro connections (__init__.py:305-355), with HERMES_KANBAN_TASK fallback for the task id — full parity with tools/kanban_tools.py:1082-1084 including the fail-open exception path. Delete the fork copy.  
Risk: Same silent-disable modes as the hallucination guard, plus two data dependencies: prometheus.db worker_results schema and the write_worker_result.py command-regex in the task body — a schema or body-template change degrades the guard to allow-all (fail-open) with no error surfaced; a periodic cron a

**kanban_tools — worker-result-written completion gate on kanban_complete**  
Status: CONFIRMED  
How: Extend prometheus-guard's _pre_tool_call: add a branch for tool_name=='kanban_complete' (currently it returns None for everything but kanban_block, __init__.py:382) that replicates _enforce_worker_result_written (tools/kanban_tools.py:470-560) — gate on HERMES_KANBAN_TASK env, read the task title from kanban.db (mode=ro, same pattern the plugin already uses for task bodies), match exp_ prefix / '[TRANSFER]' / '[STRANDED', query prometheus.db worker_results by experiment_id (exp_) or kanban_task_id ([TRANSFER]/[STRANDED]), and on a missing row return {'action':'block','message': <exact write_wo  
Risk: Fail-open parity cuts both ways: any plugin exception (locked DB, schema drift) silently allows a resultless completion — identical to the fork's behavior, but now also triggered if plugin loading itself fails; back it with a cheap cron validator (mirroring task_janitor_v2) that flags done exp_/[TRA


### Route: sidecar

**kanban_db — Built-in kanban-worker skill auto-injection with real-resolver probe**  
Status: CORRECTED → Partial: ordinary-worker lanes can leave the fork — Prometheus enqueuers put "kanban-worker" in tasks.skills atomically IN the INSERT (the independence_gate.py:498-508 idiom; guaranteed, race-free), p  
How: Mirror a1_router.py's pre-stamp pattern (a1_router.py:298-306): a cron/daemon does a guarded UPDATE appending "kanban-worker" to tasks.skills WHERE status='ready' AND skills not already containing it; upstream's own spawn code emits each entry as --skills (merge-base 7e8f50a kanban_db.py:7788), and tasks.skills is an upstream column (kanban_db.py:1755). Prometheus enqueuers (independence_gate.py:498-508 class) set skills in the INSERT for guaranteed coverage on their lanes. The sidecar performs the resolvability probe ONCE per tick (skill file present under the assignee profile's HERMES_HOME s  
Risk: INSERT→claim race means best-effort coverage — some workers spawn without the skill (tolerable: KANBAN_GUIDANCE, the mandatory lifecycle contract, is upstream system-prompt per #50473; kanban-worker is a supplementary pattern library, and this is prompt-level with measured non-compliance anyway); if

**kanban_db — Fleet-wide goal loop (bounded finalize budget) via env**  
Status: CONFIRMED  
How: Stamp upstream's own per-task columns instead of fork env: guarded UPDATE tasks SET goal_mode=1, goal_max_turns=COALESCE(goal_max_turns,6) WHERE status='ready' AND (goal_mode IS NULL OR goal_mode=0) — merge-base spawn already converts task.goal_mode into HERMES_KANBAN_GOAL_MODE=1 / HERMES_KANBAN_GOAL_MAX_TURNS (7e8f50a kanban_db.py:7737-7739), consumed by cli.py:16025. Mirrors a1_router.py's pre-stamp exactly; Prometheus enqueuers additionally pass goal_mode=True/goal_max_turns=6 to create_task (upstream params, 7e8f50a:2404-2405) for race-free coverage on owned lanes. Complementary upstream P  
Risk: Race-missed stamps → those workers run single-shot and can churn once through the reaper (healed by retry + janitor, and 96% complete on rerun anyway); the bigger hazard is a silently dead sidecar reverting the WHOLE fleet to single-shot mode with no error — needs task_events provenance rows + a wat

**kanban_db — HERMES_MODEL provenance pin for worker results**  
Status: CONFIRMED  
How: Move provenance resolution to the deployment-owned consumer: ~/.hermes/scripts/write_worker_result.py:422 (outside the repo, freely editable) resolves model as tasks.model_override looked up by HERMES_KANBAN_TASK via a mode=ro kanban.db read → fallback to the profile config model.default — the same precedence _default_spawn implements, computed at write time instead of spawn time. Add a NULL-model backfill cron joining worker_results→tasks.model_override (mirrors task_janitor_v2's post-hoc repair class) to heal stragglers. Zero fork code: the only in-repo consumer of this env pin is provenance  
Risk: Consumer-side lookup can fail mid-session (DB busy/locked) → NULL rows until the backfill cron passes (bounded by cadence); and the script's precedence logic must be kept manually in sync if the dispatcher's effective-model resolution ever changes (e.g. profile-config-routed A1 without model_overrid

**kanban_db — Per-profile-cap overflow reassignment to installed profiles only**  
Status: CONFIRMED  
How: Cron rebalancer doing UPDATE tasks SET assignee=<free profile> on ready rows whose assignee is at the per-profile cap, validating candidates via ~/.hermes/profiles/ existence (hermes_cli.profiles.profile_exists — the hard invariant from the 60k-phantom-assignee pathology recorded in _find_free_worker's docstring, fork kanban_db.py:6752-6786) and writing a 'reassigned' task_event (mirrors a1_router pre-stamp + task_janitor_v2 phantom-assignee reassignment :390-427). Unlike model_override stamping this is NOT race-limited: a capped task is deferred every tick (skipped_per_profile_capped, fork :7  
Risk: Rebalance latency = cron cadence (task idles up to one interval where the in-dispatcher version reassigns same-tick); sidecar's view of 'free' can lag the dispatcher's running map within a tick, causing a bounce; and any bug that writes a non-installed assignee recreates the orphaned-forever-in-read


### Route: runtime_patch_plugin

**cron_scheduler — MIN_GRACE raised 120s -> 300s for missed-run fast-forward**  
Status: CONFIRMED  
How: Drop the fork hunk (README already excludes it from PR-0001 as 'site tuning, not upstreamable') and replace with a tiny user plugin, e.g. ~/.hermes/plugins/cron-grace/__init__.py, whose import-time code wraps rather than replaces: `import cron.jobs as _cj; _orig = _cj._compute_grace_seconds; _cj._compute_grace_seconds = lambda schedule: max(300, _orig(schedule))`. This works because (a) the sole call site is the late-bound module-global lookup at cron/jobs.py:1601 inside _get_due_jobs_locked, so patching the module attribute rebinds every future tick; (b) load order is proven in the gateway pr  
Risk: Refactor risk is the main one and its blast radius is inherently bounded: if the patch fails or never loads, behavior reverts to upstream MIN_GRACE=120 — jobs up to 2-5 min stale fast-forward instead of catching up, i.e. degraded scheduling fidelity on the loaded box, not breakage; the guard turns t

**kanban_tools — Redaction removed from kanban_complete outputs and kanban_comment body**  
Status: CONFIRMED  
How: No knob or hook reaches it: force=True explicitly bypasses security.redact_secrets (agent/redact.py:493-495), pre_tool_call cannot rewrite args, transform_tool_result only mutates what the model sees (not the DB write), and a sidecar cannot un-redact values destroyed at write time. A prometheus-fleet plugin's import-time code CAN patch it in the right process: workers are fresh spawns that load ~/.hermes/plugins at startup before any tool executes, so at plugin import do `import tools.kanban_tools as kt; kt.redact_sensitive_text = lambda text, **kw: text` — rebinding the module-level name only  
Risk: Deviates from the fork in one spot: the fork RETAINS block-reason redaction (tools/kanban_tools.py:1067) and the module-level rebind kills that too — acceptable on this single-user fleet but a real behavioral delta; also patches only processes that load user plugins, so any code path writing complet

**prompts — MEMORY_GUIDANCE rewrite: system-facts-only memory policy**  
Status: CONFIRMED  
How: A ~/.hermes/plugins prompt-policy plugin (loaded exactly like prometheus-guard via hermes_cli/plugins.py:1305) replaces the constant at import time, which provably runs before first use: _prepare_agent_startup (hermes_cli/main.py:13867 -> 12317-12330) calls discover_plugins() at CLI startup for all agent commands, before AIAgent construction and before the first build_system_prompt. Patch BOTH bindings — agent.prompt_builder.MEMORY_GUIDANCE (source of truth, also re-exported at run_agent.py:159) AND agent.system_prompt.MEMORY_GUIDANCE (the top-level from-import at agent/system_prompt.py:33-34   
Risk: Silent-miss risk if upstream adds a new consumer that from-imports MEMORY_GUIDANCE in a module imported before plugin discovery (patch both known bindings today; the sentinel guard cannot see new import sites). Loud-fail risk: any upstream rewording of the guidance text trips the sentinel and the fl

**prompts — _MEMORY_REVIEW_PROMPT rewrite: background memory review scoped to system facts**  
Status: CONFIRMED  
How: Same prompt-policy plugin. Critical mechanic verified: run_agent.py:1550-1554 binds _MEMORY_REVIEW_PROMPT/_COMBINED_REVIEW_PROMPT/_SKILL_REVIEW_PROMPT as AIAgent CLASS attributes, and background_review.py:888-892 selects via getattr(agent, name, module_default) — instance->class MRO lookup means the CLASS attribute is the operative value and it is read at call time (review threads fire many turns after startup, turn_context.py:294-301). So patch run_agent.AIAgent._MEMORY_REVIEW_PROMPT and — closing the fork's own acknowledged defect — AIAgent._COMBINED_REVIEW_PROMPT (background_review.py:276-2  
Risk: If upstream removes the getattr seam or the class-attribute re-export (background_review.py:884-892 comment says it exists for back-compat, so it could be deleted in a cleanup), the class patch becomes dead and only the module-global fallback holds — patch both so one surviving seam suffices; sentin

---
*Source: workflow wf_015df138-e92 (64 agents; journal in the session transcript dir). Verification statuses: CONFIRMED = adversarial verifier reproduced the route against code; CORRECTED = verifier found the route wrong and the table row carries the corrected route; UNVERIFIED = verifier crashed, treat as plausible.*
