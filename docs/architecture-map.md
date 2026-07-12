# Prometheus: Architecture Map

**A persistent evolutionary mechanism-discovery system built from ordinary components.**

**Last updated:** 2026-07-08 · **History:** `docs/architecture-changelog.md` (append-only). This map is the timeless snapshot: current design and invariants only. If a sentence needs a date or describes an event, it belongs in the changelog, not here.

---

## Orientation (read this first)

Prometheus is an autonomous research loop running out of `~/.hermes` on a single Linux workstation. LLM workers run real experiments (code, GPU) against open questions; findings accumulate in SQLite; the system generates its next questions from its own results. No human sits inside the cycle. It has no relation to the Prometheus metrics/monitoring project — there is no `prometheus.yml` on this box.

Two layers share the machine:

- **Hermes** — the agent infrastructure this runs on: the cron system (`~/.hermes/cron/jobs.json`, ~86 jobs executed by a ticker with a circuit breaker — the OS crontab is **empty**, look in jobs.json; all schedules use staggered-offset cron exprs, `OFFSET-59/PERIOD * * * *` with a per-period-unique offset — hour-field steps for multi-hour jobs — so lanes never fire on the same minute; no plain `every Nm` intervals), the kanban dispatcher that spawns workers, worker profiles, and the terminal/code tooling. Jobs with `no_agent: true` run a script directly; the rest are LLM-driven with a prompt (Director, inspector, external-probe). **Near-stock Hermes since the 2026-07-08 de-fork:** the `~/.hermes/hermes-agent` checkout carries only a **small upstream-PR-able patch set** (~13 files, staged as patches in `fork-patches/upstream-prs/`); all site policy lives update-proof in **three user plugins** (`~/.hermes/plugins/prometheus-{guard,prompt-policy,runtime-tuning}` — must be wired into EVERY worker profile, not just main config), a sidecar cron (`fleet_policy_stamper.py`), and config/enqueuer columns. See §Fork modifications to the hermes-agent base.
- **Prometheus** — the research application on top: `~/.hermes/scripts/*.py`, the two databases below, and `~/.hermes/docs/` (this map; history in `architecture-changelog.md`).

Two databases, not one (both SQLite WAL, written concurrently — see §Database Concurrency):

| DB | Role | Core tables |
|---|---|---|
| `~/.hermes/kanban.db` | dispatch state | `tasks` (cards), `task_runs` (executions), `task_events`; `archived_tasks` / `archived_task_runs` = the archived legacy board (`source_board` marks provenance) |
| `~/.hermes/prometheus.db` | knowledge | `curiosities` (questions), `worker_results` (findings intake), `experiments` (accepted findings), `knowledge_claims` + `claim_evidence` (claim maturity), `transfer_tracking`, `domains` |

They link via `kanban_task_id`. First-reader traps: prometheus.db also contains an **empty legacy `tasks` and `cycles`** (superseded by kanban.db; kept because `experiments.cycle_id` declares an FK on `cycles`) and a near-dead `experiment_results` (14 rows — `worker_results` is the authoritative intake table).

A worker is an LLM agent session (xiaomi/mimo-v2.5 via OpenRouter — or **free local Agents-A1** for the transfer/exp lanes and, as a co-primary, the cross-family adversarial lane, §Background Services — up to ~20 concurrent) spawned headless by the dispatcher with the task card as its prompt. The dispatcher pins the model per spawn (`task.model_override` → dispatcher `HERMES_MODEL` env → config default; `model_override='agents-a1'` also switches the worker to the local `custom:agents-a1` provider) and `worker_results.model` records which model produced each result — per-result provenance for any by-family comparison. That column is stamped by BOTH worker_results writers: `write_worker_result` resolves it (task override → env → config default) when the caller doesn't pass one, and the `result_bridge` gap-filler resolves the same precedence from the kanban task — a NULL model means neither could resolve, not that the path skipped it. Workers are stateless; the databases are the memory.

First 60 seconds on this box:

```bash
python3 ~/.hermes/scripts/health_snapshot.py    # "is the science loop alive and honest" board
cat ~/.hermes/db_reconciliation_report.json     # cross-DB invariants (30m monitor)
ls -t ~/.hermes/cron/output | head              # recent cron activity
# dashboards: http://localhost:8888 (v1) · http://localhost:8889 (topology/calibration)
```

---

## System Overview

Prometheus is an evolutionary mechanism-discovery system. Questions are vehicles. Mechanisms are the genes. A question lasts one generation. A mechanism — "identity persistence," "selection over weighting," "temporal binding" — survives dozens of generations across unrelated domains. The system selects for transferable mechanisms, not good questions.

Workers run experiments on questions, extract candidate mechanisms from the results, and transfer those mechanisms across domains. Confirmed mechanisms generate transfer questions. Refuted hypotheses generate variable-mutation questions. Mutated questions route back into the queue. The loop does not terminate. It is a bounded branching process. Domain labels are organizational taxonomy — the actual graph connects ideas by detected mechanism similarity, not domain label.

```
Question (vehicle) → Experiment → Mechanism Candidate (gene) → Transfer (mutation) → Question (new vehicle)
```

Questions come and go; mechanisms persist. A mechanism that survives transfer across psychology → networks → machine learning → neuroscience is telling you something even before you know whether every intermediate experiment was correct.

**Worker state.** Workers are stateless between runs; the databases are not. A worker runs its experiment, writes its result to `prometheus.db`, and terminates — what it learned stays in the DB for the next worker. A worker does not reliably *pull* that knowledge on its own: it is **pushed** into the task body at creation time by the knowledge feed (§Knowledge feed & independence; the blind epistemic lanes are deliberately exempt). Parallel workers all read/write the same shared state — worker N's tasks are built against the knowledge workers 1…N−1 added. Delete all the workers and respawn them: nothing is lost. Delete the databases: the system starts from scratch. This also makes the system scale-agnostic — a worker is an API call against the shared databases; the dispatcher caps it at ~20 concurrent, but one or a thousand run from the same codebase.

## System Architecture

One closed loop through two databases, plus four attached loops. Numbered stages are
the main cycle; every arrow is a cron acting on a table, so "the system" at any moment
is just whichever crons are due.

```
   ┌─────────────────────── new questions — the loop does not terminate ────────────────────────┐
   ▼                                                                                            │
┌─ [1] QUESTIONS — prometheus.db: curiosities ───────────────────────────────────────────┐      │
│ open questions with lineage (parent_curiosity_id, evidence_depth)                      │      │
│ scored 0-100 in 4 lanes (confirm / novel / expand / break); queue front reserves       │      │
│ deep lineages + retest/boundary lanes; BENCH3 probes (known answer) enter at p4        │      │
└───────┬────────────────────────────────────────────────────────────────────────────────┘      │
        │ task_refiller (2m): pull top-scored, bake RAG confirmed-priors into the card body     │
        ▼                                                                                       │
┌─ [2] TASK CARDS — kanban.db: tasks / task_runs ────────────────────────────────────────┐      │
│ priority: 4 BENCH3 · 3 deep lineage + adversarial attacks · 2 synthesis/retests ·      │      │
│ 1 exploration · 0 residual (5 kept free for manual emergencies); execution             │      │
│ history in task_runs / task_events                                                     │      │
└───────┬────────────────────────────────────────────────────────────────────────────────┘      │
        │ kanban dispatch (10s): spawn one worker per card                                      │
        ▼                                                                                       │
┌─ [3] WORKERS — stateless LLM sessions, <= ~20 concurrent ──────────────────────────────┐      │
│ run real code (GPU torch / sklearn via transparent hook); report verdict, basis        │      │
│ (what the verdict RESTS ON), confidence (caps: 0.85 hard / 0.6 self-caveat /           │      │
│ 0.5 literature-authority); no memory between runs — the databases are the memory       │      │
└───────┬────────────────────────────────────────────────────────────────────────────────┘      │
        │ write_worker_result.py (instant; preserves artifacts) or result_bridge.py (10m)       │
        ▼                                                                                       │
┌─ [4] INTAKE — prometheus.db: worker_results (applied=0) ───────────────────────────────┐      │
│ apply_worker_results (5m): derive verdict, then QUALITY GATE (validate_quality >= 40)  │      │
│ pass  -> experiments + claim attachment + mechanism extraction + lineage + manifest    │      │
│ reject -> marked applied=1 with NO experiments row (normal — no zombie experiments)    │      │
│ epistemic-lane outcomes route back here: retest credit (block 1g), ATTACK_OUTCOME      │      │
│ routing (NARROWED spawns a boundary question), arbitration verdicts, boundary closure  │      │
└───────┬────────────────────────────────────────────────────────────────────────────────┘      │
        │                                                                                       │
        ▼                                                                                       │
┌─ [5] KNOWLEDGE — prometheus.db: experiments · knowledge_claims ────────────────────────┐      │
│ claim maturity recomputed every 15m (CANDIDATE → REPLICATED → ESTABLISHED; DISPUTED    │      │
│ on contradiction); promotion gates: independent retest (REPLICATED needs >=1) +        │      │
│ spurious-agreement + answer-level adjudication + circularity cap + adversarial         │      │
│ survival (ESTABLISHED must survive an attack); is_meta + is_empirical_fact             │      │
│ splits keep self-measurement and lookup-facts off the science tiers;                   │      │
│ calibrated confidence per result; RAG index over experiments feeds [2] bodies          │      │
└───────┬────────────────────────────────────────────────────────────────────────────────┘      │
        │                                                                                       │
        ▼                                                                                       │
┌─ [6] FOLLOW-UP GENERATION — new rows in curiosities ───────────────────────────────────┐      │
│ CONFIRMED → transfer question (new domain, or one rung up the abstraction ladder)      │      │
│ REFUTED → variable-mutation question (which variable flips it?)                        ├──────┘
│ REPLICATED claim → adversarial attack · NARROWED → boundary question →                 │
│ mapped scope → scoped re-attack · retests: CANDIDATE wsc>=2 + REPLICATED               │
│ n_formal=0 · arbitration (4 sources incl. false consensus) · lit residues              │
│ + synthesis_merger (10m) · compression clustering of mature claims (30m) ·             │
│ opportunity / cross-domain injection (5m) — all throttled at 250 new questions/h       │
└────────────────────────────────────────────────────────────────────────────────────────┘

  stores: [1][5][6] prometheus.db (knowledge) · [2][3] kanban.db (dispatch) · [4] bridges them
  artifacts: every gate-passer leaves code+results+manifest in ~/.hermes/artifacts/<task_id>/
  box [2] priorities are steady-state; temporary dial raises are covered by the
  Drain mode note under Pipeline mechanics

  Attached loops — they enter and exit the cycle at the numbered stages:

┌─ EXTERNAL TRUTH — BENCH3 (validation path #1) ─────────────────────────────────────────┐
│ external-probe (1h, LLM): web-search fresh findings → true/false questions with        │
│ known_answer → enter [1] at priority 4 → workers answer blind at [3] → verdicts        │
│ scored against truth → calibration_loop (15m, benchmark rows ×50) retrains the         │
│ confidence model (calibration_audit, 30m, checks it) → domain_accuracy.json →          │
│ boosts [1] scoring for domains < 85% accuracy and aims the next probes at them         │
└────────────────────────────────────────────────────────────────────────────────────────┘
┌─ MECHANISM SURVIVAL + META-LEARNING (validation path #2) ──────────────────────────────┐
│ transfer_tracking follows every cross-domain transfer: queued → task_created →         │
│ completed_cross_domain. A mechanism surviving unrelated domains is structural          │
│ validity — independent of BENCH3. (Fragility lives at the CALL SITES, not in           │
│ transfer_tracking.py — see Transfer & Abstraction.) Meta: auto_tune (15m) +            │
│ outcome_routing (5m) apply the system's own confirmed findings to its own              │
│ routing — exploration of the exploration process.                                      │
└────────────────────────────────────────────────────────────────────────────────────────┘
┌─ EPISTEMIC GATES — challenge evidence before it becomes belief ────────────────────────┐
│ retest lanes (detector): CANDIDATE wsc>=2 + REPLICATED n_formal=0 → reserved           │
│ dispatch (refiller lanes) → credit at intake (block 1g + reconciler safety net)        │
│ answer_consistency_adjudicator (15m): supports asserting incompatible answers          │
│ under matching CONFIRMED labels → DISPUTED                                             │
│ circularity_critic (30m): target built from its own detection features → capped        │
│ adversarial attacks (15m, cross-family models): BROKEN disputes · SURVIVED             │
│ credits ESTABLISHED · NARROWED → boundary question → claim_scopes → scoped             │
│ re-attack (SURVIVED_WITHIN_SCOPE terminates the refinement loop)                       │
│ dispute_arbitration (15m, 4 sources): adjudicator pairs · replication disagree-        │
│ ments · adversarial breaks · false consensus (SA) — losers retracted, re-tiers         │
│ literature lanes: novelty_audit (metadata only) → residues become questions ·          │
│ CONTRADICTED → re-derivation attack · NOT_FOUND → discovery_spotlight hardening        │
│ self-scrutiny: meta_claim_prober risks self-beliefs in unseen domains ·                │
│ symbolic_verifier checks closed forms · cross-domain disconfirmation gate              │
│ independence gate (self-arming): prior-fed agreement → shelf support-depth haircut     │
└────────────────────────────────────────────────────────────────────────────────────────┘
┌─ SUPERVISION — observes both DBs, repairs, never generates questions ──────────────────┐
│ L4 watchdog-of-watchdogs (systemd, 2m): cron freshness, kills stuck scripts            │
│ L3 janitors: task-janitor (10m) · workspace-gc (15m) · rag-index-guard (15m) ·         │
│    artifact-maintenance (15m)                                                          │
│ L2 monitors: evidence-integrity (30m) · db-reconciliation (30m) · calibration-         │
│    audit (30m) · selfref · source-epistemic · queue/topology · BENCH3 gap (6h)         │
│ L1 the loop itself — a stall surfaces as queue depth / heartbeat drift                 │
│ + Director (LLM, 5m) strategy · inspector (LLM, 2h) · dashboards :8888 / :8889         │
│ + backups: prometheus.db (15m) · kanban.db (30m) — online-backup API + quick_check     │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Pipeline mechanics

- **Task creation** (`task_refiller.py`, 2m): maintains queue depth from scored curiosities (4 lanes), BENCH3 ground truth, synthesis outputs, compression synthesis, and opportunity injection — `direct_db_create` fast path when ready < target, else `batch_create_tasks.py` (full dedup pipeline). Kanban priority via `get_task_priority(item, lane)`, steady-state tiers: **5** RESERVED (kept vacant for manual emergencies) · **4** BENCH3 (external truth) · **3** deep lineage (the depth-climb) + adversarial attacks + arbitration · **2** synthesis/compression + candidate-retests · **1** exploration mass (transfer/refiller/injection) · **0** residual. Kanban dispatches by `ORDER BY priority DESC, created_at ASC` — but that governs dispatch *among already-created ready tasks*; which curiosities get *created* into tasks is front-first over the queue with a small per-cycle budget (genuine-lane budget 1-2 tasks/cycle), which is why the low-volume epistemic lanes need reserved, **budget-exempt** sub-pools (`_LANE_TARGETS`; full circuit in §Retest & boundary lanes). The knowledge feed + `prior_feed_stamp.record()` run on every task at creation (§Knowledge feed & independence). Worker-spawned tasks (`kanban_create` from inside a session) default to priority 0 and would never dispatch under perpetual p1-p4 refill: `task_janitor_v2.normalize_spawned_priorities` (10m) re-tiers ready p0/NULL tasks older than 10 minutes whose TITLE matches a known lane pattern (BENCH3→4, retest→retest tier, synthesis→2, transfer/boundary/lit-residue→1 — same text rules as `get_task_priority`); unrecognized titles stay at 0, so the residual tier remains real for genuinely unclassifiable work. **Decomposition children are the second p0-starvation class:** the in-gateway auto-decomposer's child titles ("Design computational experiment…") match no lane pattern, and `decompose_triage_task` used to create them priority-less — every auto-decomposed family (children + the root waiting in `todo`) parked forever. Fixed at the source (fork `kanban_db.py`: children inherit the ROOT task's priority) with a janitor heal for strays: `normalize_spawned_priorities` re-tiers ready/todo p0 tasks whose `created` event carries `from_decompose_of` to the root's priority.
- **Queue composition** (`sync_curiosity_views.py`, 5m): materializes the queue from SQLite into `self_state.json`. Front-to-back reservation order: (1) **deep lineage** — up to 20 items at `evidence_depth >= 10` from ANY source, absolute front; (2) **candidate-retest** — up to 32 `[CANDIDATE-RETEST]` items behind the deep block (this positions them; actual dispatch still depends on the refiller sub-pools — the deep block ahead is an effectively infinite front, re-topped every sync); (2b) **boundary** — `[BOUNDARY]` items behind the retests, same low-volume-lane circuit (queue-front cap 8); (3) hidden bridges (moderate_success, refutation_boost, hidden_bridge_injection); (4) protected injection (opportunity_injection_70); (5) other genuine; (6) regular transfers — capped at 70% of the queue. Auto-retires four categories: low-score (score < 25, age > 3d), NULL-score age > 3d (**exempt:** deep lineages `evidence_depth >= 8` and `narrowed_boundary` curiosities — both wait on small reserved lanes, so "un-scored after 3 days" means un-*reached*, not worthless), orphaned inactive (age > 3d), corrupted status strings. Auto-resolves items marked [RESOLVED]/[COVERED].
- **Deep-lineage retire exemption (`DEEP_FLOOR = 8`):** deep items are still pruned on a *real* low score; only the NULL-score-on-age heuristic is disarmed for them. `DEEP_FLOOR = 8` matches the intake gate/throttle exemption threshold (`apply_worker_results.py`), keeping the deep-protection boundary consistent system-wide; `COALESCE(evidence_depth, 0)` treats NULL depth as shallow (still prunable).
- **Scoring** (`curiosity_scorer.py`): source_quality (0-50) + diminishing (0-25) + outcome (0-25) + novelty (0-10) + diversity (0-10) + transfer_bonus (+3) + analogical (0-20) + break_bonus (+12 to −5) + evidence_depth_bonus (depth×3, max +15) + fertility_bonus (max +15; logistic regression on lineage data) + accuracy_boost (up to +16 for domains below 85% BENCH3 accuracy). Capped at 100. Reads domain break-informativeness + disagreement rates from `replication_state.json` via a freshness-guarded loader (falls back to the live `replication_results` column when the snapshot is stale > 1h or missing).
- **Multi-objective scorer** (`score_curiosities.py`, 10m): writes p_confirm/p_novel/p_expand/p_break (four logistic-regression objectives) for stale items; task selection sorts per-objective (top-N each lane, sized by portfolio weight). Three bounded, default-neutral adjustments on top: a per-domain **surprise bonus** (≤ 12, purely additive, exploration lanes only) for domains whose preregistered priors sit below the global confirmation baseline (reads `prior_override_status.json`, n ≥ 10 floor; never lowers a score); a **meta-transfer haircut** multiplying `p_confirm` for `[TRANSFER]`-shaped questions (= 1 − min(0.25, overconfidence_rate/2), floor 0.75, no-op below 20 scored probes; reads `meta_transfer_calibration.json` — the system's measured record predicting its own transfer, written by `meta_claim_prober.py`); and a **mechanism-shape haircut** for over-trusted shapes (reads `mechanism_calibration.json`, classifies via `classify_mechanism`; = 1 − min(0.15, gap_pp/200), floor 0.85, n ≥ 200 + gap ≥ 3pp gates). Shape and transfer haircuts compose multiplicatively under an overall p_confirm floor of 0.70.
- **Portfolio allocator** (`portfolio_allocator.py`, 10m): 4-dimension budget across confirm/novel/expand/break; live weights in `~/.hermes/portfolio_state.json`.
- **Generation throttle** (`generation_throttle.py`): new curiosity generation capped at 250/hour (benchmark questions exempt). Without it the system generates questions faster than it answers them, backlogging surface-level questions; the throttle forces deeper lineages. Checked by all four generators (apply_worker_results, synthesis_merger, compression_synthesis, inject_opportunities).
- **Intake** (`apply_worker_results.py`, 5m): derives verdicts; **quality gate** `validate_quality >= 40` — rejects are marked `applied=1` with NO experiments row (normal, not lost data); updates experiments + claims, extracts mechanisms, links BENCH3 results to curiosities, resolves the parent curiosity (CURIOSITY_ID → text match → source_experiment → auto-create root); evidence gate (wsc >= 1.0) and generation throttle both exempt deep lineages (parent depth >= 8); routes epistemic-lane outcomes back into the gates (§Claim Lifecycle); generates follow-ups — CONFIRMED → transfer question (horizontal domain crossing or vertical abstraction ascent; worker-generated follow-ups take priority, auto-generation fires only when < 2 exist), REFUTED → variable-mutation question (§Transfer & Abstraction, refutation branching).
- **Drain mode (temporary dial raises):** the four epistemic-lane dials — `task_refiller._LANE_TARGETS` (steady state: retest 8, boundary 4), the sync queue-front boundary cap (8), attack `MAX_PENDING` (3, cards p3), arbitration `MAX_PENDING` (4, cards p3) — get raised temporarily when a backlog needs draining, then reverted. `health_snapshot.py` GATES carries the drain-complete tripwire (retest-blocked ≈ 0 ∧ boundary pool ≈ 0 ∧ unattacked-REPLICATED ≈ 0); `revert_drain_surge.py --apply` restores every steady-state value in one pass (refuses unless the tripwire is met; exact-match edits, per-file backups, py_compile, auto-restore). This map documents steady state everywhere — if live code disagrees with a dial value here, check the tripwire and the changelog before "correcting" either side.

---

## Transfer & Abstraction

The system moves in two dimensions:

- **Horizontal (transfer):** mechanism tested in a new domain. Psychology → network science. Questions change; the mechanism is tested under new conditions. Transfer is the system's primary reproductive mechanism.
- **Vertical (abstraction ascent):** specific phenomenon → general mechanism → meta-mechanism:

```
identity persistence (specific)
    ↓
attention capture (general mechanism)
    ↓
transferability through standardization (meta: how mechanisms transfer)
    ↓
feature selection over weighting (meta: how the system selects)
    ↓
temporal binding (meta: how mechanisms persist across time)
```

Horizontal transfer tests a mechanism's breadth; vertical abstraction tests its depth. Both accumulate in the deepest lineages. Lineage analysis shows the ladder is more bidirectional than the one-way model suggests: PROPERTY questions (named behaviors) act as a translation layer between MECHANISM and DESCRIPTOR, and lineages oscillate between naming a behavior and formalizing it (P↔D) rather than permanently ascending. See `scripts/abstraction_ladder.py`.

**Transfer state taxonomy** — what a transfer question carries across, five states:

| State | Definition | Examples |
|-------|-----------|---------|
| **DESCRIPTOR** | Mathematical/statistical pattern | "power law," "Pareto optimality," "universality class" |
| **PROPERTY** | Named behavior or observation | "selectivity advantage," "timing effect," "dominance pattern" |
| **MECHANISM** | Causal process | "ionophore binding," "attention dilution," "feedback cascade" |
| **DOMAIN_PAIR** | Domain-to-domain without named concept | "Does biology transfer to drug resistance?" |
| **AMBIGUOUS** | Truly unresolvable | Malformed questions, domain-specific jargon |

PROPERTY is the hub — the only state heavily connected to both MECHANISM and DESCRIPTOR, and it tends to appear earliest ("what happens?" before "why?"). Winning lineages switch states (M→D, D→M, P→P) rather than locking into one.

**Question morphology** — question shape determines fertility (a 12x gap between templates):

| Template | BF | Fertile % | Reproduces? |
|---|---|---|---|
| Transfer (can/does) | 0.78-0.81 | 36-38% | Yes |
| How-does | 0.32 | 14% | Weakly |
| What-is | 0.17 | 9% | Weakly |
| Does-variable (non-transfer) | 0.13 | 6% | Barely |
| Mechanism-recursion | 0.07 | 7% | No |
| Boundary-conditions | 0.07 | 3% | No |

Transfer questions mutate one variable and test it in a new domain — they naturally suggest the next mutation. Mechanism-recursion questions ask for explanations of explanations — they collapse inward and terminate. This is a morphological constraint on the question *generator*, not a scoring or routing concern.

**Refutation branching** (in `apply_worker_results.py`): refuted hypotheses generate follow-ups directly — a verdict-to-mutation path distinct from synthesis — using only the fertile morphologies (the old mechanism-recursion / boundary-condition templates were terminal by construction and are gone):

- **REFUTED**: "Which specific variable, if changed, would flip the result?" + "[TRANSFER] Does this behave differently in a neighboring domain?"
- **PARTIALLY REFUTED**: "Which parameter showed partial support, and does strengthening it help?" + "[TRANSFER] Does the partially-supported component transfer?"
- **REFUTED_SETUP**: "Can this be tested with a simplified design using fewer variables?"

**Question lineage:** every curiosity links to its spawner via `curiosities.parent_curiosity_id` (NULL for seeds; parent resolution tries four methods in order: CURIOSITY_ID from the kanban task body → text match → source_experiment lookup → auto-create a root curiosity, so synthesis/cross-domain/tagless tasks still link). **`curiosities.evidence_depth`** is *experiment-backed* depth: parent's depth + 1 **only when the step is backed by a real run** (child minted from a worker result, or its parent question actually answered via `resolved_by_experiment`); synthetic hops (synthesis, injection, question mutation) inherit the parent's depth unchanged — they carry depth, they do not earn it. The raw hop count lives in `curiosities.generation_depth` (+1 unconditionally; lineage bookkeeping only). Both computed by `compute_evidence_depth.py` (5m, `--recent` = last 2h). Everything that consumes depth — score bonus, deep queue tier, `DEEP_FLOOR` retirement exemption, the health DEPTH panel — keys on `evidence_depth`, i.e. verified progress, never on generator recursion.

**Transfer validation — two independent paths:**

1. **BENCH3** — external ground truth: web-sourced true/false questions with known answers validate individual experimental correctness (loop in the diagram above).
2. **Transfer survival** — a mechanism that survives transfer across multiple unrelated domains is evidence of structural validity, independent of BENCH3.

BENCH3 validates answers; transfer survival validates mechanisms — they measure different things. **Instrumented caveat:** the domains are less independent than "unrelated" implies. Every worker shares one base model, and the knowledge feed pushes each confirmed finding into the next same-domain task — correlating the very experiments whose agreement is counted as survival; the ~6.9pp cross-domain confirm/disconfirm asymmetry (§Evidence Integrity) is the fingerprint of that shared common cause. Survival's weight scales with the *independence* of the confirmations, not just their count — measured and priced in §Knowledge feed & independence. The deepest common cause (the shared base model) no amount of internal agreement removes, and BENCH3 anchors *answers*, not whether a simulated threshold corresponds to anything in the world — the toy-vs-world question has its own lane (§Discovery shelf, world grounding) and no internal gate.

**Meta-learning:** the system applies its own confirmed findings about transfer to its own routing — `auto_tune.py` (15m) + `outcome_routing.py` (5m). Exploration of the exploration process; the learning accumulates across lineages.

**Transfer tracking pipeline** (`transfer_tracking` table, three stages — `queued → task_created → completed_cross_domain | completed_same_domain`):

1. **Insert** (`insert_tracking`, called by `apply_worker_results.py`): a worker result containing `[TRANSFER from X]` inserts a row (`source_result_id`, `source_domain`, `target_domain` placeholder `__pending__`, `status='queued'`); the `lineage_live` path also inserts when a child curiosity's experiment domain differs from its parent's.
2. **Task created** (`update_task_created`, called by `task_refiller.py`): links the kanban task to the tracking row — matches by `source_result_id`, fallback `update_task_created_by_domain` (source+target domain, FIFO) when source_result_id is NULL.
3. **Completed** (`update_completed`, called by `apply_worker_results.py`): matches by `task_id`; writes `destination_result_id`, `destination_domain`, final status.

**Recurring failure mode:** the module's functions are self-contained and correct; the fragile part is the **call sites**. A guard that can't be met, a missing import, or a stale governance block silently strands `queued` rows and `health_snapshot.py` shows zero completions. When diagnosing a stuck pipeline, inspect the call site, not the module.

---

## Database Concurrency

`prometheus.db` is a single SQLite file written concurrently by cron jobs, worker profiles, and the Director. WAL mode. Writers serialize; a waiter that gives up sooner than the longest writer transaction dies with "database is locked" — so both sides are bounded:

- All DB-writing cron scripts use `db_retry.get_db()` — `busy_timeout=30s` plus a 6-attempt exponential retry ladder (~3 min total endurance). Intake and worker heartbeats use `prometheus_db.get_db()` (contextmanager, auto-commit on clean exit) — same 30s busy_timeout. **Do not lower these:** 800ms/5s timeouts were the cause of ~146 cron deaths/day.
- `get_db(db_path=None, readonly=False)` supports any DB path and readonly mode.
- **Never hold a write transaction across a pass, a batch, or any LLM/network call.** Python's sqlite3 auto-opens a deferred transaction at the first write and holds the WAL write lock until commit. The discipline in the long-running writers: `apply_worker_results.py` commits per result; `contradiction_detector.py` commits after each pass; `maturity.recompute_all_maturity` buffers status changes during its ~70k-row scan and applies them in one short end transaction. A new multi-minute writer must follow the same pattern or it will starve every other writer.
- `health_snapshot.py` GATES line `db lock failures (24h)` is the tripwire for this class: healthy ≈ 0, > 5/day means a long-transaction writer is back.
- Do NOT switch journal modes or VACUUM while live.
- **`scripts/prometheus_db.py` resolves `DB_PATH` via `os.path.expanduser("~/.hermes")`, never `dirname(__file__)`** — the module gets copied around by workers, and a dirname-relative copy landing in `scripts/` once pointed intake + janitor at a ghost `scripts/prometheus.db` (pre-migration schema) and crash-looped both. Location-proof resolution is the invariant; if a stray copy reappears, quarantine it, don't "fix" the canonical one.

### Data conventions & quirks

- `worker_results.applied`: 0 = awaiting intake, 1 = processed. **`applied=1` with no experiments row is normal** — that is how the intake quality gate marks rejects (score < 40, "no zombie experiments"). 275 such rows predate the frozen monitor baseline; `db_reconciliation_monitor.py` (30m) alerts only if the pre-baseline count *grows* (that would mean experiments rows were deleted — the failure class behind the past DB corruption).
- Timestamps in prometheus.db are epoch floats — including `claim_status_history.changed_at` and `knowledge_claims.created_at`/`last_updated_at`. **Affinity trap:** those two knowledge_claims columns are declared TEXT, so SQLite stores the epoch floats as uniform numeric-STRINGS ('1783032140.0'); any numeric comparison on them MUST `CAST(col AS REAL)` — a bare TEXT-vs-number comparison in SQLite is always true. Lexical order still equals numeric order (fixed 10-digit integer part). The trigger `trg_experiments_require_id` hard-rejects NULL/empty `experiments.id` at insert.
- Claim text has two distinct fields: `knowledge_claims.hypothesis_text` is the **question/prompt** the claim was created from (copied verbatim from `experiments.hypothesis`) and is the claim's IDENTITY — `claim_hash` and `normalized_text` grouping derive from it, so **never rewrite it**. `claim_summary` is the **human-readable finding** (the answer) and is what every display should prefer, falling back to `hypothesis_text`. Filled from the best `claim_evidence.key_finding` by `refresh_claim_summary()` in `claim_lifecycle.py` (live: per-result in `attach_experiment`; batch: `compute_all_posteriors`; one-shot: `backfill_claim_summary.py`). The selector ranks by **provenance first** — an `ARBITRATION_VERDICT:` or `ATTACK_OUTCOME:` supersedes an ordinary finding, so a NARROWED/regime-split correction headlines a claim rather than the confident original it refuted-in-scope — then dominant-verdict type, confidence, recency. Because hypothesis_text is the prompt, it is often question-shaped, can be the *opposite polarity* of the finding ("Does X break?" → "X HOLDS"), and may carry task-orchestration tags leaked from task titles; the summary sidesteps both. Analysis code (adjudicators, critics, classifiers, dedup) correctly keys off hypothesis_text — only *displays* prefer claim_summary.
- Provenance tags: `BACKFILLED_FROM_KANBAN` in `tags` marks rows recovered by the kanban backfill (see changelog); `EMPTY_UNRECOVERABLE` in `quality_tags` marks experiments whose result text is lost for good (excluded from the empty-result alert).
- Worker confidence is hard-capped at 0.85 at both write paths (§Confidence Calibration). Two further mechanical caps: findings that admit no independent verification ("cannot independently replicate…") cap at 0.6 (`quality_validator.caveat_confidence_cap`, tag `CAVEAT_CONF_CAPPED`, never a score penalty); `verdict_basis = literature_authority` caps at 0.5 (citing a paper is a report, not a verification).
- `worker_results.verdict_basis` / `experiments.verdict_basis`: what the verdict rests on — `independent_computation | replication | simulation | literature_authority`. Mandatory `--basis` in the worker contract; parsed from a `BASIS:` line in older findings; NULL on pre-field results.
- **Artifacts**: every gate-passing experiment leaves `~/.hermes/artifacts/<kanban_task_id>/` with its evidence files (preserved at result-write time, while the worker is still alive — workspaces are destroyed at task completion) plus `manifest.json` (verdict, basis, confidence, quality, hypothesis, full finding, frozen at intake).
- `knowledge_claims.is_empirical_fact`: 1 = a LOOKUP FACT — a named real-world event VERIFIED or an established value recalled (instrument/agency as SUBJECT of an observation verb; "Did <entity> detect/win/approve/launch"; recall/verify of physical/CODATA/NIST constants, or a MODEL correctly recalling/reproducing WELL-KNOWN facts/values/identities — the #45169 costume, a capability-recall lookup regardless of how the card frames itself — shared detector `discovery_routing.is_lookup_fact`, verb/value-anchored with a ≤60-70-char proximity window so a method question that merely names constants is not flagged), not a mechanism DISCOVERED. Tagged at claim creation by `empirical_fact_classifier.py` and re-stamped at literature-audit time by `novelty_audit.py`. Kept and matured normally, but excluded from `health_snapshot.py` headline science tiers (the `_NONSCI` filter, `*_fact` companion counts) and from all discovery-candidate queries — "what has it DISCOVERED" is never answered by a lookup. Maintained like is_meta: creation-time tag + one-shot backfills, no cron.
- `knowledge_claims.is_meta`: 1 = claim about the system itself (transfer-rate bookkeeping, `[COMPRESSION]` clustering, component claims — `meta_claim_classifier.py`). Kept and matured normally; excluded from headline science tiers (`*_meta` companions report them).
- `knowledge_claims.circular_construction`: 1 = supporting experiments are self-fulfilling by design (`circularity_critic.py`); maturity caps such claims at CANDIDATE. NULL = not yet reviewed.
- `knowledge_claims.method_code_mismatch`: 1 = a MAJORITY of cross-family judges found the supporting CODE does not implement the METHOD the claim's finding describes (`method_code_alignment_critic.py`, ScientistOne CoE-audit check I4); maturity caps at CANDIDATE. NULL = not yet reviewed. Cron `method-code-alignment` (30m `27-59/30`, majority-vote over the preserved head+tail code excerpt, per-experiment).
- `adversarial_replications` table: attack tasks against REPLICATED claims — status `survived | refuted | narrowed`, `attacker_model` = the cross-family model the attack dispatched on. Semantics + routing in §Adversarial attacks & claim scoping.
- `claim_scopes` table: mapped-regime ledger, keyed `UNIQUE(claim_id, source_curiosity_id)`. Posterior-neutral, deliberately NOT `claim_evidence` (the orphan sweep would eat it; a scope is context, not support). Written at boundary-task intake; consumed by scoped re-attack cards (§Adversarial attacks & claim scoping).
- `novelty_audits` table: literature-check verdicts for promoted claims, plus `corroborated` / `finder_model` / `finder_found` / `search_adequacy` / `novel_residue`; mirrors the verdict to `knowledge_claims.prior_work_status='LIT_'+verdict`, posterior/tier untouched. Mechanics in §Literature & novelty lanes.
- `experiments.kanban_task_id`: durable backlink to the kanban task that produced the experiment, copied from `worker_results.kanban_task_id` at intake. It is load-bearing for task-level joins after kanban archival (prior-feed stamps, clean-room/world ledgers, reconciliation); if NULL while a unique worker_result backlink exists, backfill it rather than inferring from titles.
- `task_prior_feed` table: durable per-task record of whether a task carried the RAG confirmed-priors feed (`prior_fed`, `n_fed`, `fed_hashes`), keyed by `kanban_task_id`, written at creation for every task. Lives in prometheus.db — NOT on the kanban task, whose body is nulled on archival. §Knowledge feed & independence.
- `claim_independence` table: per-claim independence ledger (`independence_ratio`, `n_stamped`/`n_stamped_fed`, `families`, `single_family`, `independence_multiplier` bounded 0.5–1.0). Posterior-neutral; consumed by the discovery shelf. §Knowledge feed & independence.
- `claim_near_duplicates` table: between-claim redundancy ledger (`claim_a`/`claim_b`, `similarity`, `tier` duplicate/related, `status_a`/`status_b`). Written by `claim_similarity.py` (Qwen3-Embedding-0.6B cosine over promotion-band claims — a claim's identity is its hypothesis hash, so the same finding asked two ways mints two claims that both promote). Posterior-neutral, full-refresh each run; report-only (a merge is an operator decision — two duplicates can hold different verdicts). Surfaced as Discoveries §4.
- `synthesis_curiosity_links` table: provenance ledger for synthesis-generated questions (`synthesis_output_id`, `synthesis_task_id`, `curiosity_id`, `method`, `created_at`). Forward-populated by `synthesis_merger.py` at the exact curiosity insert; historical rows are backfilled only when an exact proposal-text match is unique inside the time window. It links what was actually inserted — skipped/deduped/throttled/repaired proposals are not guessed.
- `worker_results.predicted_direction` doubles as the PREREGISTRATION channel: the fleet-native form is `{"<hypothesis_key>": +1|-1}` — a signed direction committed before the experiment ran, symmetric with `worker_results.observed_direction`. `prior_override_report.py` scores it against the OBSERVED sign on the same key (framing-independent — a `-1` is a negative direction, not a "refuted" verdict), not against the binary `hypothesis_supported`. An explicit `{"prediction":…}` form, if present, scores against `hypothesis_supported` instead.
- `answer_adjudications` table: answer-level consistency verdicts for promotion-band claims (`answer_consistency_adjudicator.py`) — two CONFIRMEDs asserting incompatible answers DISPUTE the claim instead of counting as replication.
- `dispute_arbitrations` table: decisive-experiment tasks that settle stored contradictions and false consensus. `claim_evidence.evidence_type = 'retracted_by_arbitration'` marks evidence on the losing side — excluded from wsc and every promotion-band query, never deleted. §Dispute arbitration.

---

## Claim Lifecycle

Claims are tracked by artifact verification status:

```
HISTORICAL_UNKNOWN — not yet checked
UNVERIFIED         — queued for artifact check
VERIFIED           — all artifact files present and valid
VERIFIED_PARTIAL   — some files present but incomplete
FILES_MISSING      — referenced files not found on disk
FAILED             — artifact check failed (malformed, empty, etc.)
```

### Computed Maturity

Claim maturity tiers are **derived properties computed from provenance signals**, not stored state advanced through transitions. `contradiction_detector.py` runs every 15 minutes and recomputes `claim_status` for every non-exempt claim from scratch — there are no promote/demote transitions; the status is what the evidence says it is. The pure function (`compute_maturity()` in `scripts/maturity.py`) evaluates:

| Signal | Column | Threshold |
|--------|--------|-----------|
| Evidence quantity | `weighted_support_count` | 0.5 (CANDIDATE), 2.0 (REPLICATED), 3.0 (ESTABLISHED) |
| Contradictions | `contradiction_count` | > 0 → DISPUTED. Recomputed from the live dispute bases every cycle (`maturity.recompute_contradiction_count`): disagreed replications, worker support/refute splits, refuted adversarial replications, unresolved contradictory adjudications. Writers' inline increments are immediate marking only — the recompute is canonical, so a dispute whose basis is retracted or arbitrated clears itself. |
| Refutes | `refute_count` | must be 0 for REPLICATED/ESTABLISHED |
| Independent retest | `n_independent_retests` | >= 1 for REPLICATED |
| Formal replication | `n_formal_replications` | >= 1 for ESTABLISHED |
| False consensus | `spurious_agreement` | < 0.6 for ESTABLISHED. Actively drained by the SA settlement lane (§Dispute arbitration, source 4); the score reads only non-retracted evidence, so settling actually opens the gate |
| Circular construction | `circular_construction` | = 1 → hard cap at CANDIDATE (set by `circularity_critic.py`) |
| Method-code mismatch | `method_code_mismatch` | = 1 → hard cap at CANDIDATE (set by `method_code_alignment_critic.py`, majority-vote) |
| Blind (non-prior-fed) support | `task_prior_feed` via `worker_results.kanban_task_id` | >= 1 blind stamped support for ESTABLISHED (`established_blind_supports`) — ONLY once `independence_armed.json` says the stamp is proven AND the claim has >= 2 STAMPED supports (`blind_gate_min_stamped`; pre-stamp claims exempt). Gate, not weight. Settlement: the blind retest lane + the clean-room lane mint stamped-blind supports (§Knowledge feed & independence) |
| Adversarial survival | `adversarial_replications` status `survived` | >= 1 for ESTABLISHED (`established_break_survivals`). Only `survived` counts; only `refuted` disputes. Outcome semantics in §Adversarial attacks & claim scoping |

Thresholds are externalized in the `MATURITY_THRESHOLDS` dict (epistemic policy, not implementation constants). The function returns a `MaturityResult` with `passed_checks`, `failed_checks`, and `blocking_reason` for explainability. `n_independent_retests` and `n_formal_replications` are recomputed from `replication_results` at the top of every detector cycle (`maturity.recompute_n_independent_retests` / `recompute_n_formal_replications`). Legacy statuses (WELL_KNOWN, KNOWN, NOVEL, PARTIAL) and the `MERGED` tombstone are exempt from recompute.

**`MERGED` is a tombstone, not a maturity tier:** duplicate hypothesis-fragments (the same question re-created under a pre-fix `normalize_hypothesis` that didn't strip retest/transfer tags before hashing) are collapsed by the opt-in `merge_duplicate_claims.py` — each loser's live provenance (claim_evidence, adversarial_replications, claim_scopes, answer/circularity adjudications, meta_transfer_predictions) is re-pointed BY ROWID onto a survivor, the loser is set `claim_status='MERGED'` with a `merged_into` pointer, and the survivor carries the canonical claim_hash so future retests attach to it instead of forking. MERGED is exempt from the recompute (stays inert) and excluded from every claim_status shelf filter; the survivor's tier is re-derived from the union of re-pointed evidence. History tables stay on the loser (re-pointing a row's transition history would fabricate one). Fully reversible via the `claim_merge_undo` ledger.

**Status history:** every status change is recorded in `claim_status_history` with old/new status, blocking reason, and a snapshot of the provenance signals at the time — the audit trail for debugging oscillation.

**DISPUTED is a computed state, not a one-way door:** the count is recomputed from live bases every cycle, so a dispute whose basis disappears clears on the next cycle — and disputes are also actively settled (§Dispute arbitration). **Every dispute class — and every promotion blocker — has both a self-clearing count and an active settlement path; the maturity ladder has no one-way doors.**

The `weighted_support_count` mechanism demotes claims whose evidence has evaporated (orphaned rows, deleted worker_results); demotion and orphan cleanup run every 15 minutes. External quality control is the calibration loop (§Confidence Calibration).

### Retest & boundary lanes

**Credit (demand side):** when a `[CANDIDATE-RETEST]` task completes, intake block 1g writes the `replication_results` row — `replicated` for a supporting verdict, `disagreed` for a refuting one (which also routes the claim to DISPUTED via the replication-disagreement detector) — and resolves the retest curiosity. Block 1g is a best-effort *fast path*: its curiosity lookup opens a per-result kanban connection that intermittently loses to lock contention (≈ half of credits under load), so `reconcile_retest_credits.py` (15m at :08, before the detector) is the **guarantee** — one batched kanban read credits every completed retest 1g missed (`candidate_retest_reconciled`, `INSERT OR IGNORE` for the race) and resolves its curiosity. `replication_results` has `UNIQUE(original_experiment_id)`: one credit row per source experiment, so `n_independent_retests` counts at most one retest per claim (the gate needs ≥ 1).

**Supply side — two injectors** in `contradiction_detector.py`:

- `inject_candidate_retests`: CANDIDATE wsc ≥ 2.0, world claims only, no retest yet; quota 5/cycle. Dedups against **active** retest curiosities only — a closed-uncredited curiosity (age-retired: retest curiosities carry score=NULL; self-resolved; lost worker; transient intake failure) simply becomes re-injectable in wsc-DESC order. Every closer-without-credit is self-healing churn, not a permanent lockout.
- `inject_formal_replication_retests`: REPLICATED claims stuck at `n_formal_replications = 0` after a `disagreed_arbitrated` retest (an independent retest, but not a passing formal replication); quota 3/cycle. Targets a support experiment with **no existing credit row**, since the UNIQUE constraint + refiller moot-skip silently drop a credit aimed at an already-credited source.
- The old REPLICATED-no-retests injector is **RETIRED** (its population is empty under computed maturity; the adversarial attack lane supersedes it).

**Dispatch — a low-volume lane against the ~18k-item deep-lineage front needs all four:** (1) a queue-head slice from `sync_curiosity_views.py` (the deep block ahead is re-topped every sync — position alone never dispatches); (2) a reserved refiller sub-pool (`_LANE_TARGETS` in `direct_db_create`); (3) exemption from the per-cycle creation budget (genuine-lane budget is 1-2/cycle — an unexempted lane starves at the budget check, not the queue); (4) adequate kanban priority (dispatch is `ORDER BY priority DESC`). Steady-state lane targets: retest 8, boundary 4 in flight (raised in Drain mode). Each lane self-throttles to a trickle once its deficit stays 0. Bulk lever for a gate-blocked backlog: the `flood_retest_queue_20260703.py` pattern — mint a ready task per blocked claim in one pass (same dedup/moot rails), then let the standing sub-pool hold equilibrium against inflow.

**Resolution guard:** `resolve_curiosities.py`'s Jaccard matcher skips retest-provenance curiosities entirely — their text quotes the source experiment's hypothesis, so any text-match close is false. Block 1g and the reconciler are their only legitimate closers.

**Boundary lane:** a NARROWED attack outcome spawns one `[BOUNDARY]` boundary-mapping curiosity per claim (provenance `narrowed_boundary` — exempt from NULL-score age retirement) so the discovered regime boundary becomes a question instead of dying in finding prose. At completion, intake writes the mapped regime to `claim_scopes` and the claim becomes re-attackable *with scope*. Boundary-spawn dedup is ACTIVE-only (same lesson as retests: an any-status-ever dedup permanently locks out claims whose question age-retired).

### Adversarial attacks & claim scoping

`adversarial_replication_enqueuer.py` (15m) enqueues one break-lane attack task per REPLICATED claim (cap 3 outstanding, cards p3 — raised in Drain mode; 48h expiry; self-throttles as the unattacked pool empties), wsc ≥ 3.0 claims first (ESTABLISHED-ready on survival), with the claim's preserved original code in the card. **NARROWED-attractor terminal cap:** a claim narrowed `MAX_NARROWS_BEFORE_TERMINAL` (=5) times with no survival is attack-saturated and dropped from `get_targets` — its regime is mapped and further attacks only re-find incidental nuance (a forward guard against the narrow→boundary→re-attack orbit; #65826 reached 21/0 before this). The `--claim` re-examine lanes are exempt (deliberate targeted challenges). Cards demand `ATTACK_OUTCOME: BROKEN | NARROWED | SURVIVED` (scoped re-attack cards add `SURVIVED_WITHIN_SCOPE`); intake routes via the `ADVERSARIAL_REPLICATION_FOR_CLAIM:` marker:

- **BROKEN** → `refuted`: disputes the claim, then adversarial arbitration (§Dispute arbitration).
- **NARROWED** → `narrowed`: neutral — no dispute, no survival credit, stays attack-eligible; spawns the `[BOUNDARY]` question (§Retest & boundary lanes).
- **SURVIVED** → `survived`: credits the ESTABLISHED gate.
- **SURVIVED_WITHIN_SCOPE** (scoped cards only) → `survived`, appends the stated incidental limit to `claim_scopes`, spawns NO boundary question. This is convergence semantics: without it every incidental nuance pulls the verdict to NARROWED ("no meaningful narrowing" reads as absolute) and a claim whose core keeps holding orbits the narrow→map→re-attack cycle forever instead of promoting. NARROWED on a scoped card is reserved for a boundary that MATERIALLY shrinks the mapped regime.

**Scoping circuit:** the enqueuer skips claims with an open `[BOUNDARY]` curiosity — map the boundary first, then re-attack. Re-attack cards carry the latest `claim_scopes` rows as a MAPPED SCOPE block ("attack WITHIN the mapped regime; an out-of-regime failure is already known and is NOT a new NARROWED") — this is what makes SURVIVED reachable for claims whose every attack had re-discovered the same hole.

**Cross-family attackers:** a same-family attack shares the prior that made the claim, so attacks dispatch on other model lineages via kanban `model_override` — `ATTACKER_POOL` weighted `deepseek/deepseek-v4-flash` 40% + **`agents-a1` 40%** (the free local Qwen3.5 — always-on and quota-free, so it is the *reliable* cross-family backbone the budget-bound free tier cannot be; deepseek stays as the unlimited cloud surge-valve) + a 20% free-tier slice split across four families (nemotron-ultra / gpt-oss-120b / llama-3.3-70b / gemma-4-31b, 5% each; the OpenRouter free quota is 1000 req/day account-wide, so the free share is budget-bound). **`pick_attacker` is independence-gated:** it reads the claim's supporting families from `claim_independence.families` and drops any attacker whose lineage already backs the claim (`agents-a1` and `qwen:free` are both `qwen`), so no family ever grades its own work. Side-effect worth knowing: deepseek — having attacked most claims already (a survived attack is logged as `deepseek` support) — is now excluded from re-attacking them, so A1 (the fresh lineage) absorbs the slack (measured ≈52% A1 / 14% deepseek / 33% free; brand-new claims still open ~40/40). It still rotates per re-attempt; failed free-tier attacks expire → re-pool → rotate (self-healing). The `a1_router` deny-list keeps the *router* from auto-routing attacks to A1, but the enqueuer's explicit stamp bypasses it and dispatches via `_default_spawn`; `clear_failed_a1_pins` skips deny-listed tasks so a failed A1 attack is never demoted to the mimo default (= same-family). `adversarial_replications.attacker_model` records the intended attacker; `--stats` prints the per-attacker survival ledger + intended-vs-executed reconciliation.

**`--claim <id> --note "<challenge>"`** targets a specific claim regardless of tier (`[RE-EXAMINE]` card), skipping only claims with a live pending attack — the shared entry point used by the literature-contradiction, cross-domain-disconfirmation, meta-prober, and spotlight-hardening lanes to re-try an already-promoted claim against a challenge its same-family attackers never confronted. **Targeted picks are reliability-restricted but independence-gated:** `pick_attacker(reliable_only=True)` draws from `RELIABLE_ATTACKERS` (deepseek + local A1) minus the claim's supporting families, widening to the free tier only when both are excluded. (This replaced a hard force to pool[0]=deepseek that bypassed the independence gate on every targeted challenge — deepseek re-examining claims its own survived attacks corroborated, A1 starved at 0 picks despite its 40 weight; since the targeted lanes are ~all current attack volume once the unattacked pool drains, the force was the whole mix.)

### Dispute arbitration

`dispute_arbitration_enqueuer.py` (15m) turns each stored contradiction into a decisive experiment (SIDE A vs SIDE B; verdict `A_CORRECT | B_CORRECT | BOTH_WRONG | REGIME_SPLIT`; cap 4 outstanding, cards p3 — raised in Drain mode), drawing from **four sources** merged by wsc:

1. **Adjudicator pairs** — answer-level incompatibility stored by `answer_consistency_adjudicator.py`. Clears because a terminally resolved arbitration masks the adjudicator-basis term.
2. **Replication disagreements** — SIDE A = the original experiment, SIDE B = the disagreeing retest (findings verbatim from `replication_results`). Settles rows in place: `disagreed` → `disagreed_arbitrated` (the contradiction basis term counts only `disagreed`; the rows still count as independent retests).
3. **Adversarial breaks** — SIDE A = the claim's supporting evidence, SIDE B = the attack. Settles `refuted` → `refuted_arbitrated`; `B_CORRECT` retracts support evidence, `A_CORRECT` returns the claim to REPLICATED still needing a genuine `survived` attack.
4. **False consensus** (the SA settlement lane) — targets claims that are not DISPUTED at all: REPLICATED with a survived attack whose supports agree on the flag while diverging in substance (`spurious_agreement >= 0.6`). Sides split by the SA signal itself (`_split_sa_sides`: direction split, else magnitude-decade split, else text/flag mismatch); retraction of the losing side drops the score on the next recompute, since `spurious_agreement.py` reads only non-retracted evidence.

On resolution: losing evidence rows become `evidence_type='retracted_by_arbitration'` (excluded from wsc and all promotion-band queries, never deleted); the claim returns to its evidence-determined tier on the next maturity recompute — still subject to the adversarial-survival gate. REGIME_SPLIT retracts nothing; the worker submits one boundary question per regime through the normal `--queue` path. Outcomes route at intake via the `DISPUTE_ARBITRATION_FOR_CLAIM:` marker.

**Pre-triage** (`classify_recomputable_dispute`): a dispute that is a bare numeric disagreement (same quantity, differing values, NO conditional/interpretive language) gets a specialized RECOMPUTE card ("re-derive the quantity directly") instead of the open-ended decisive-experiment card (recorded in the `triage` column). The recompute card keeps the FULL verdict contract, so a misroute self-heals — a conditional dispute comes back REGIME_SPLIT. **Recompute model:** recompute cards run on the fleet default (mimo). A free-coder slice (`RECOMPUTE_CODER`, once ~1 in 4 first-attempt cards on `qwen/qwen3-coder:free`) was **disabled** — the free model does the re-derivation but exits without calling `kanban_complete`, so its cards churned dozens of protocol-violation → reblock cycles each (compounded while the task-janitor abandon-guard was blind to reaper blocks, §Key Scripts). `RECOMPUTE_CODER = None` no-ops the guarded slice; re-enable only on a protocol-reliable model. `dispute_arbitrations.model_override` still records any per-card override.

### Evidence Integrity

Three schema columns support evidence-quality checks (plus `verdict_basis`, `circular_construction`, `is_meta`, and the `adversarial_replications` / `answer_adjudications` tables — §Data conventions & quirks):

- **`knowledge_claims.n_independent_retests`** (INTEGER DEFAULT 0): count of `replication_results` rows linked via claim_evidence → experiment. Conservative join (broken evidence chains excluded). Used by the REPLICATED maturity gate.
- **`knowledge_claims.n_formal_replications`** (INTEGER DEFAULT 0): count of `replication_results` rows with `replication_status='replicated'`, same join pattern. Used by the ESTABLISHED maturity gate. Recomputed every 15m.
- **`claim_evidence.is_cross_domain`** (INTEGER DEFAULT 0): 1 when the evidence row's domain differs from the claim's domain. Not acted on in weighted_support_count; the cross-domain discount decision is NOT applied (4.9pp BENCH3 accuracy gap, below the 5pp threshold). If ever used for weighting, the asymmetry runs in one direction only — cross-domain evidence is equally reliable for confirmation (92.0% vs 92.8%, < 1pp gap) but less reliable for disconfirmation (65.7% vs 72.6%, 6.9pp gap). Any scheme that treats the flag symmetrically will be wrong. Re-measured every 6h (`bench3_cross_domain_remeasurement.py`).

`claim_lifecycle.py` includes a dedup guard on INSERT INTO claim_evidence: the UNIQUE INDEX `ux_claim_evidence_dedup` on `(claim_id, key_finding)` enforces deduplication atomically at the DB level; INSERT OR IGNORE absorbs constraint violations from concurrent workers silently. See `docs/evidence_integrity_sequence.md` for the full sequence and `scripts/fix_evidence_integrity.py` for the migration script.

### Spurious-Agreement Gate

The posterior/claim_status machinery reads only the binary `hypothesis_supported` flag, so a claim could reach the top tier on supports that all voted "yes" while describing mutually contradictory magnitudes or directions. `spurious_agreement` (a per-claim score in `knowledge_claims`, recomputed every 15m by `spurious_agreement.py`) measures this: high when supports agree only on the flag while disagreeing in substance — text/flag mismatch ("REFUTED" written but flag=supported), direction split, or effect-size magnitudes spanning orders of magnitude. It never alters posterior/claim_status by itself; it is an input to `compute_maturity()` (ESTABLISHED requires < 0.6), recomputed before the maturity recompute each cycle so the gate always reads fresh. `fetch_claim_rows` excludes `retracted_by_arbitration` evidence (the same rule wsc follows) — so when the SA settlement lane retracts the divergent side, the score actually drops and the gate opens.

**What SA cannot see — and the gates that close the gaps.** SA measures *diversity of expression*, so two unanimity failures pass straight through it: (1) methodologically identical circular experiments look diverse (canonical case: claim #3839, every support fabricating its target from its own detection features, SA 0.393) — closed by `circularity_critic.py` (`circular_construction=1` → capped at CANDIDATE); (2) supports asserting *incompatible answers* under matching CONFIRMED labels register as agreement unless the magnitude spread happens to trip (claim #67000, contradictory closed forms, SA 0.25) — closed by `answer_consistency_adjudicator.py` (answer-level comparison → `contradiction_count` → DISPUTED). With the adversarial-survival requirement on top, "a claim cannot sit in the highest-confidence tier on an illusion of unanimity" is enforced by four independent gates, not asserted by one score. A fifth gate sits on a *different* axis — **method fidelity**, not unanimity: `method_code_alignment_critic.py` (ScientistOne CoE-audit check I4, added 2026-07-07 from a field-gap analysis; `method_code_mismatch=1` → capped at CANDIDATE) fires when a majority of cross-family judges find the supporting CODE does not implement the METHOD the finding describes — the numbers can be real and reproduce while the method is misreported (prose says "bitwise encoding", code runs a plain set). Report-verified clean on the current shelf; it stands as a backstop, majority-voted (the reasoning judge is non-deterministic) with per-experiment scoping + a head+tail code excerpt (top-only truncation hid the `__main__` block and produced false "stub code" flags in calibration).

---

## Confidence Calibration

### Calibrated confidence (system-level)

`calibrated_confidence` is the model-estimated probability that a worker supports a claim. Raw worker confidence is anti-predictive: the calibration model (StandardScaler → LogisticRegression → sigmoid) learns near-zero weight on raw confidence and relies on evidence features instead.

**Scalar calibration map (fallback):** `calibrate_confidence()` in `write_worker_result.py` maps raw confidence to empirical probability via a Beta-smoothed per-bin map (0.05 grid, K=20 pseudo-count; canonical values in `scripts/calibration_map_opt2.json`; Brier 0.186 vs 1.186 raw). The map is **intentionally non-monotonic**: the 0.95 bin calibrates to ~0.47 — LOWER than the 0.80-0.85 bins (~0.78-0.80) — because highest-self-confidence claims are empirically the least reliable (majority refuted).

**External validation:** `calibration_audit.py` (30m) checks calibrated_confidence against benchmark questions with known answers — scoring only typed-ground-truth rows on the correct axis (effective_confidence = cal for known=true, 1−cal for known=false; cal measures P(supported), not P(verdict-correct)). `external-probe` (1h) keeps the ground-truth supply flowing (BENCH3 loop in the diagram).

**Closed calibration loop:** `calibration_loop.sh` (15m) trains a challenger each cycle on consensus + truth labels (benchmark rows weighted 50x) and promotes it only if it beats the champion on AUC/Brier. After each promotion, `backfill_calibration_mv.py` recomputes `calibrated_confidence` for the whole table. **Evidence-count sourcing trap:** the backfill joins a worker_result to its claims through `claim_evidence`, NOT through `knowledge_claims.first_experiment_id` — the latter only matches when the experiment was the claim's *first*, scoring every later evidence row as zero-support, and since `net_support` is the model's strongest feature that crushes genuinely-supported rows to near-zero.

**confidence_change (experiment-level, NOT a belief delta):** `experiments.confidence_change` stores **sign-corrected raw worker confidence** — `+worker_confidence` for supported verdicts, `−worker_confidence` for REFUTED/PARTIALLY-REFUTED/REFUTED_SETUP (`apply_worker_results.py`). It is NOT a Bayesian posterior delta: near-zero means a low-confidence worker, negative means a refuted verdict. The genuine belief-movement signal is `knowledge_claims.posterior` relative to the 0.5 prior; using `confidence_change` as a "dead weight / beliefs-not-moving" proxy is a trap — it penalizes humble workers and suppresses refutations.

### Raw confidence enforcement (worker-level)

Individual worker confidence is capped at **0.85**, enforced at two independent write paths:

- **Direct CLI:** `clamp_confidence()` in the canonical `write_worker_result.py` and inline validation in all 82 worker profile copies. Values > 0.85 are **hard-rejected** (ValueError, exit 1). Percentage normalization preserved (50→0.5, 85→0.85, 95→rejected).
- **Bridge:** `cap_confidence()` in `result_bridge.py` (10m cron). Workers who report via `kanban_complete` metadata bypass `write_worker_result.py` entirely — the bridge reads metadata directly and INSERTs into `worker_results`, using a **silent clamp** (> 0.85 → 0.85) because the worker has already completed and cannot resubmit.

The argparse `--confidence` default is **0.5**; in practice workers choose explicit values clustering 0.70-0.80. The KANBAN_GUIDANCE in `prompt_builder.py` provides four calibration anchors within the enforced range — the code enforces the ceiling, the guidance describes what the numbers mean. Higher confidence (0.90+) is reserved for system-level aggregation across multiple independent replications; it is not claimable by a single worker result.

---

## Knowledge feed & independence

**The feed (push, not pull):** when a task body is built, the RAG index is queried for related prior experiments (`experiment_rag.query_rag`, FAISS, ~0.12s/call); positive-verdict findings (text starting `CONFIRMED`/`SUPPORTED`, score > 0.5, self-negating "REFUTED/DOES NOT" excluded) are baked into the body — top ~3 — under a "CONFIRMED PRIOR FINDINGS (build on these, do not re-test; … strong priors, not ground truth)" block. Fires on **every** task; fails open if the index or GPU embed server is down or nothing clears the filter. Both creation paths carry it: `task_refiller.py` (worker_id `refiller_enrichment`) and `batch_create_tasks.py` `generate_task_body` (worker_id `batch_enrichment`, same filter). Forward-propagation tradeoff, accepted: wrong-but-confident findings are caught downstream by replication/contradiction/DISPUTED, not pre-filtered here.

**Blind lanes:** `[CANDIDATE-RETEST]`, `[BOUNDARY]`, and `[CLEAN-ROOM]` cards skip the feed entirely (`prior_feed_stamp.SUPPRESS_PREFIXES` / `suppress_prior_context()`, checked by both builders) — a retest fed the original conclusion is not an independent retest. Their stamps read `prior_fed=0`, so their results count as measured-blind supports.

**The epistemic cost, measured not assumed:** pushing a confirmed finding into later same-domain tasks *correlates* the experiments whose agreement is later counted as validation (transfer survival, replication) — and independent confirmation is only worth its independence. So:

- **`prior_feed_stamp.py`** (library, called at creation on EVERY task, best-effort try/except with a 5s timeout — never blocks creation) durably records whether each task carried the feed → `task_prior_feed` (in prometheus.db, because kanban bodies are nulled on archival; the signal was retrospectively unrecoverable, which is why the forward stamp exists).
- **`independence_gate.py`** (`independence-check` cron, 10m, `--check --apply`) MEASURES per promotion-band claim: prior-fed fraction (from the stamp, kanban-body fallback), model-family diversity, cross-domain share. The shelf measures as a model monoculture (~64% single-family; cross-family support comes mostly from the deepseek and — since the co-family change — local-A1 (qwen) audit/attack lanes). **Self-arming haircut — behind a CHECK, not a clock:** `--check` audits the stamp against live kanban bodies and arms the instant accuracy ≥ 0.98 over ≥ 50 audited AND ≥ 500 stamps (sticky; refuses on drift) → `independence_armed.json`. When armed, `--apply` writes a bounded per-claim `independence_multiplier` (all-fed stamped support → 0.5, all-blind → 1.0; only for claims with ≥ 2 STAMPED supports, neutral elsewhere) to `claim_independence`, consumed by `discovery_spotlight.score()` as a support-depth haircut. **The WEIGHT stays confined to the discovery shelf; the maturity core carries independence as a GATE** (ESTABLISHED needs ≥ 1 stamped-blind support once armed — §Computed Maturity). Gate-not-weight boundary. Reports to `~/.hermes/independence_report.json`; the `≥ 1 non-simulation basis` rule is reported-not-gated (`verdict_basis='simulation'` is near-unused, ~7/1945 supports).
- **Clean-room lane** (`clean-room-lane` cron, 30m, `--enqueue --limit 2`): for ESTABLISHED single-family claims, a `[CLEAN-ROOM]` card carrying ONLY the hypothesis (feed-free, explicit do-not-look-up-priors instruction), cross-family `model_override` (deepseek; env `HERMES_CLEANROOM_MODEL`), p3, cap 4 live, stamped `prior_fed=0` — the result lands as a MEASURED-BLIND support via normal intake hash-collision. Ledger `clean_room_replications`; dedup is live-task-only and skips claims already holding a stamped-blind support. This is the settlement path for the maturity blind-support gate.

**Honest limits, printed every gate run:** the stamp is forward-only; family independence is weak (cross-family models share training data); the deepest common cause is the shared base model, which no amount of internal agreement removes; and toy-vs-world needs external grounding (§Discovery shelf).

---

## Literature & novelty lanes

**`novelty_audit.py`** (two lanes, both `--concurrency 4 --time-budget 100`: ESTABLISHED 2h `--limit 8`, REPLICATED 15m `--limit 12`) literature-labels promoted claims KNOWN / PARTIALLY_KNOWN / NOT_FOUND / CONTRADICTED via deepseek-v4-flash + the OpenRouter `web` plugin (cross-family, effort high). Writes `prior_work_status='LIT_'+verdict` + citation; full provenance (citations, `novel_residue`) → `novelty_audits`. **Never touches posterior/tier — grounding is metadata; a paper never demotes a claim, only a computational BROKEN does.** NOT_FOUND is a candidate pool, not a prize.

- **Burden flip:** a self-graded NOT_FOUND is weak evidence of novelty, so the audit then runs an independent FINDER — a THIRD model family (`google/gemini-3.1-flash-lite` + web; `HERMES_FINDER_MODEL` override; deliberately the cheap -lite tier for a mechanical search-and-cite job, while the `--adjudicate` judge stays on full flash) tasked to LOCATE the paper, not confirm its absence. A hit overrides the verdict to KNOWN (`corroborated=0`, `finder_found`=citation); a miss sets `corroborated=1` **only when the structured index actually returned papers (`index_hits ≥ 1`)** — two families searched and neither found it, the only state that earns the FULL novelty weak-prior. A throttled/blind search (the free S2/OpenAlex pools 429 and budget-exhaust readily) leaves it UN-corroborated (0.4 discount), never crediting novelty it could not verify — which is also the honest state for a bespoke claim whose exact finding has no indexed referent. This only withholds credit; it can never mint a false KNOWN. `search_adequacy` +0.1 when ≥ 5 indexed papers were actually reviewed. Fail-soft: any finder error leaves the primary verdict untouched.
- **Operational constraint:** the OpenRouter account's data-policy settings exclude ALL `openai/*` endpoints — any openai/* lane 404s until the account settings change. Keep finder/judge models off openai/*.
- **Scholarly-index leg** (`scholar_search.py`): one cheap FINDER_MODEL call generates 3 deliberately cross-disciplinary queries (own vocabulary / neighboring-discipline vocabulary / phenomenon name), then **Semantic Scholar** (~200M, CS/biomed, indexes arXiv) + **OpenAlex** (~250M, social science/org theory) hits become prompt context the finder READS instead of recalls. Free/keyless; dedup by DOI-else-title; hard 8s wall-clock budget; per-API global politeness intervals (S2's shared unauthenticated pool 429s readily → globally serialized ~1.1s; OpenAlex polite-pool mailto) so concurrent audits can't hammer the pools. Fail-soft everywhere: any error → fewer/zero hits, callers proceed web-only. CLI probe: `python3 scholar_search.py "query"`. **Known porosity:** corroboration is only as good as LLM+web+index retrieval; cross-disciplinary counterparts are the blind spot (a confirmed finder+corroborator double-miss exists — see changelog); operator corrections are append-only rows.
- **Throughput:** one audit is ~26–106s against a 120s unattended cron timeout, so `--time-budget` stops launching new audits before the kill (commits per verdict). `--concurrency N` runs a bounded pool, but the web plugin rate-limits concurrent searches (429 → backoff serializes), so realized throughput ~3–4 audits/min regardless — the web plugin is the ceiling, not the code. `--claim N --force` re-audits one.
- The audit also stamps `is_empirical_fact` at audit time (closing the lookup leak going forward). CONTRADICTED targets the claim's CORE assertion, not an incidental figure — and it is not one actionable category: it splits into computable (→ re-derivation attack), already-ladder-resolved (reconcile the stale label), and incidental-detail (fix the label, nothing to attack).

**`novelty_residue_injector.py`** (30m): turns audit residues into questions — the loop's other direction. A `novel_residue` on a PARTIALLY_KNOWN/NOT_FOUND audit is the system's own frontier (not in prior work → most needs scrutiny); injects one follow-up curiosity per un-injected residue in the two FERTILE morphologies: "[LIT-RESIDUE] Claim #N asserts <residue> — reproduce it, find the one variable that breaks it, test whether it transfers." Lineage via `first_experiment_id` → `resolved_by_experiment`; exact dedup via a `residue_injected` flag on `novelty_audits`.

**`novelty_contradiction_attacker.py`** (30m): converts a CONTRADICTED audit into the one thing that can actually demote — a computation. Finds promoted (`is_meta=0`, not RETIRED/DISPUTED) claims with a CONTRADICTED audit and no contradiction-attack yet; enqueues a re-derivation via `adversarial_replication_enqueuer.py --claim`, handing the worker the literature's SPECIFIC disagreement (audit explanation + first citation) with instructions to re-derive from first principles and NOT defer to the paper: BROKEN ⇒ literature right, claim demoted · NARROWED ⇒ regime-limited · SURVIVED ⇒ claim upheld despite the paper. Ledger `contradiction_attacks` dedups per claim. Rare verdict ⇒ usually a no-op.

**`recall_audit.py`** (`recall-audit-guard` cron, 30m `27-59/30`, `--mechanical-only --limit 40 --time-budget 60`): the INVERSE audit — `--adjudicate` checks the novel shelf; this checks the **KNOWN bin** (claims routed off-shelf on a finder citation, which `discovery_routing` bins PERMANENTLY — a fabricated citation silently deletes a discovery). Two layers, split by MEASURED reliability:

- **MECHANICAL** (regex on citation FORM — self-referential to the system's own experiment ids / `Claim NNNNN` / "Internal…Repository" / a search engine, or a non-locatable "Multiple sources" hand-wave): high-precision, no LLM/network → **auto-un-bins** (append-only NOT_FOUND, corroborated=1, citation cleared). This is the cron — the forward guard.
- **SEMANTIC** (index-armed judge, mirror of the adjudicator): measured **68% false-alarm** against hand-vetted KNOWNs (it treats bespoke-claim specific numbers as "the finding," so it calls textbook relationships "not in literature"; idx=0 on most bespoke claims leaves it on pure recall) → **FLAGS only, never auto-acts** (`SUSPECT`/`OK` in ledger `recall_audits`; `--report` = review queue; `--confirm <id…>` = operator un-bin). The judge earns auto-act only if it calibrates — gate-not-weight again.
- Known gray zone, deliberately left binned: `ATTACK_OUTCOME: NARROWED/SURVIVED` claims that cite the very paper they critique — the citation is real and the claim isn't a restatement, but a critical re-analysis of one paper is not a novel mechanism. Dedup by audited audit-row id (each finder KNOWN checked once).

---

## Discovery shelf

**`discovery_routing.py`** (library, pure, network-free): the discovery gates as a shared router — first-match cascade → `(route, reason)`, reading only text the claim's own subsystems produced (summary, residue, audit citations, **adversarial-replication prose** — where a worker's own "PMID …" confession lives — mapped scope, prior-work citation):

1. **known_in_lit** — a PMID/DOI/arXiv id or set citation appears in the claim's own evidence (a first documentation cannot cite the paper it precedes);
2. **empirical_fact** — the verb/value-anchored lookup shape (`is_lookup_fact`, shared with the classifier);
3. **derivable** — the card's own "MATHEMATICAL IDENTITY" / "analytically proven" / "geometric property of the representation";
4. **search_miss** — low-confidence NOT_FOUND;
5. else **discovery**.

Plus `simulation_flag` (residue is entirely a self-designed Monte Carlo → kept but novelty-zeroed + labeled) and `central_quantity_drift` (same labeled quantity disagreeing across headline / residue / scope — ranges compared as intervals, refuted-prediction "Side A … REFUTED" spans stripped; #58935/#65186). `python3 discovery_routing.py` runs a self-test asserting the externally-flagged reference claims route correctly — run it after any router change.

**`discovery_spotlight.py`** (1h): the discovery TERMINUS. A `LIT_NOT_FOUND` claim at REPLICATED/ESTABLISHED is the highest-stakes item on the shelf — a genuine discovery XOR an artifact, with no textbook to catch which. Every candidate is first ROUTED (`route_claim`); only the `discovery` route is scored and hardened — no experiments spent on textbook identities or already-cited papers. `score()` is **ROBUSTNESS 0–100**: 45 tier + 25 break-survivals + 15 support-depth + 15 novelty-as-a-discounted-weak-prior (zeroed for off-shelf or model-internal-simulation claims) — **novelty is a GATE, not the pillar**; separation of robustness from novelty from triviality is the design. A bounded **decisiveness** multiplier (`decisiveness_factor`) then ranks a crisp resolution above a REGIME_SPLIT ("it depends") at equal robustness — 0.85 when the split *dissolves* into a modeling choice ("depends on the data-generating model"), 0.92 for a boundary-map ("the sides measure different quantities"), 1.0 for decisive A/B_CORRECT and ordinary findings (default-neutral; regime-splits measure ~2× over-represented at the shelf top). `get_candidates` enforces `is_empirical_fact=0`. The novelty term: `corroborated=1` on the latest audit earns the FULL weak-prior (audit confidence) instead of the 0.4 single-search discount, and the whole term rides under `NOVELTY_CEILING` — the measured shelf-level trust from the latest `novelty_calibration --adjudicate` run (env `HERMES_NOVELTY_CEILING`; 1.0 no-op until an n ≥ 8 adjudication exists). `score()` also reads the per-claim `independence_multiplier` (§Knowledge feed & independence) as a bounded support-depth haircut. Hardening: `--claim` re-derivation attacks for discovery-route candidates (for a REPLICATED candidate this doubles as the ESTABLISHED break-survival gate). Ledger carries `route`/`route_reason`; `--report` writes `~/.hermes/discovery_candidates.json` (`shelf`/`off_shelf` built from the same fresh routing pass the page renders, so `n_shelf` matches). Non-destructive throughout.

**`discovery_report.py`** (60m): renders the ledger into the **Discoveries plate** (`docs/prometheus-discoveries.html` + `~` mirror). **Re-routes every candidate at RENDER time** so a ledger row written before a router change is still filtered — the shelf holds only the `discovery` route, ranked by robustness. Sections: **§1 the shelf** (top cards + ledger table, each with a `classify_mechanism` contribution tag, full evidence trail: cleaned finding — display-only marker strip, never mutates hypothesis_text/claim_summary — tier, wsc/retests/formal, adversarial record, mapped scope, literature verdict + residue, artifacts pointer, sim-internal flag, headline/residue drift flag incl. un-negated polarity splits; negations are stripped first, a value the card quotes only to REFUTE (a "Side A/B … REFUTED" span, #58935) is stripped too, quantities are compared as intervals across finding/residue/scope so ranges like "p=2-3 vs p=0.25" fire (#65186) while an overlapping range/point does not, and a side asserting BOTH polarities is regime prose and never fires); **§2 what the router moved off the shelf**, with reasons (a survivors-only shelf hides its own errors); **§3 the gauntlet at work** (terminal kills); **§4 likely search misses**. Turquoise-plate contract: no external requests, NO JS, system fonts, build-time render; cross-linked with both topology plates. Read-only.

**`novelty_calibration.py`** (CLI): measures how often the shelf is wrong and feeds it back. `--self-audit` (default; offline, no cost) re-routes the whole shelf and asserts zero regression leaks (a claim on the shelf its own router would bin), reports residual sim-flags + drift → `~/.hermes/novelty_calibration.json` — **the guard to run after any router change**. `--adjudicate --limit K` (network; judge `google/gemini-3.5-flash`, index-armed via `scholar_search.py` so the judge reads real indexed papers instead of trusting a web-search miss) has an independent strong model check novelty AND triviality on the highest-confidence shelf claims → **false-novelty rate** → recommended novelty-credit ceiling (`1 − rate`), consumed live as `NOVELTY_CEILING`. `--history` lists past runs.

**`world_grounding.py`** (`world-grounding-lane` cron, 30m, `--enqueue --limit 2`, reconcile-first): the **toy-vs-world lane** — every other gate tests agreement between this system's runs; this one tests correspondence with the world. REPORT-ONLY stage: binds nothing until ~20 outcomes accumulate (the threshold has since been crossed — `world_agreement_rate` now EXISTS in `world_calibration.json`, ~0.7 over 20-odd verified outcomes — but the lane deliberately stays report-only until verification holds up over more). `--scan` measures the simulation-internal share of the shelf (~98%). ENQUEUE: `[WORLD]` cards (blind lane, deepseek `model_override`, p2, cap 3) task a worker to test a discovery-route shelf claim against REAL published data — provenance (name + URL/DOI) declared in the finding, loader code preserved; `WORLD_OUTCOME: HOLDS | FAILS | NO_DATASET` (NO_DATASET is first-class — it maps the toy-boundary); preregistered on `{"world_holds": ±1}`. DETECT: `world_basis()` classifies preserved artifact code (external / mixed / local_file / synthetic / no_code) — a HOLDS/FAILS counts as VERIFIED only with a declared dataset AND data-I/O in the code. RECONCILE: ledger `world_groundings` + `~/.hermes/world_calibration.json` (`world_agreement_rate` = verified HOLDS / (verified HOLDS + FAILS)). Results also ride normal intake as ordinary evidence (a FAILS disputes via the standard basis). Promotion path when the number matures: Discoveries-plate/GATES surface → spotlight gate → (maybe) maturity coupling.

---

## Compression / Structural Synthesis

Every 30 minutes (`compression_synthesis_run.py`):

1. Pulls all ESTABLISHED/REPLICATED/DISPUTED claims (CANDIDATE excluded), **with their verdict/disagreement signal**: support_count, refute_count (net_support), contradiction_count, claim_type (DIRECTIONAL/NON_DIRECTIONAL), spurious_agreement, first_experiment_id.
2. Embeds them via GPU (Qwen3-Embedding-0.6B, 1024-d, port 9150).
3. Clusters by cosine similarity (threshold 0.30).
4. Identifies clusters spanning ≥ 8 domains as bottlenecks; computes per-cluster disagreement metrics (n_disputed, n_net_negative, n_high_spurious_agreement).
5. Injects questions back into the curiosity queue, **framed by whether the cluster agrees**: disagreement present → states the conflict and asks whether the cluster is one mechanism or merged conflicting results; no disagreement → the unify-or-split framing.
6. Attaches each injected question to its lineage parent by **exact key** (exemplar claim's `first_experiment_id` → `curiosities.resolved_by_experiment`) and records actual insert provenance in `synthesis_curiosity_links` (`synthesis_outputs.id`/task id → `curiosities.id`).

This is the primary path from accumulated claims back to new questions. Clustering is on text embeddings; the verdict/disagreement signal shapes framing and labeling, not the geometry.

---

## Key Scripts

~86 cron jobs: ~83 script-only (fixed schedule) + 3 LLM-driven (Director, inspector, external-probe — state injected via preprocessor scripts). Deep documentation lives in the sections above; this table is the inventory.

| Script | Schedule | What it does |
|---|---|---|
| `director.py` | 5m (LLM) | Director: queue curation, health signals, strategic oversight |
| `task_refiller.py` | 2m | Queue depth, kanban priorities, reserved lane sub-pools, knowledge feed + stamp — §Pipeline mechanics, §Knowledge feed & independence |
| `batch_create_tasks.py` | (called) | Full dedup task-creation pipeline; same feed + stamp (`generate_task_body`) |
| `apply_worker_results.py` | 5m | Intake: verdicts, quality gate, claims, mechanisms, lineage, follow-up generation, epistemic-lane outcome routing — §Pipeline mechanics |
| `write_worker_result.py` | (worker CLI) | Instant result write; artifact preservation; confidence clamp; scalar calibration map |
| `result_bridge.py` | 10m | Bridges `kanban_complete` metadata into worker_results; silent confidence cap |
| `compute_evidence_depth.py` | 5m | `evidence_depth` (experiment-backed) + `generation_depth` (raw hops), `--recent` = last 2h |
| `synthesis_merger.py` | 10m | Merge findings into synthesis outputs; skips saturated domains; batch cap 50/run; records actual synthesis→curiosity provenance in `synthesis_curiosity_links` |
| `backfill_synthesis_curiosity_links.py` | (CLI, on demand) | Backfill `synthesis_curiosity_links` only for unique exact proposal-text/time-window matches; reports skipped ambiguous/no-match proposals rather than guessing |
| `create_synthesis_tasks.py` | 5m | Synthesis task creation (auto-synthesis) |
| `contradiction_detector.py` | 15m | The maturity recompute cycle: claim_status, contradictions, SA recompute, retest injectors, orphan cleanup, artifact_status normalization — §Computed Maturity, §Retest & boundary lanes |
| `maturity.py` | (imported) | `compute_maturity()` pure function, signal recomputes, `MATURITY_THRESHOLDS` — §Computed Maturity |
| `spurious_agreement.py` | (imported) | Per-claim false-consensus score — §Spurious-Agreement Gate |
| `quality_validator.py` | (imported) | `validate_quality` intake gate + caveat confidence cap |
| `claim_lifecycle.py` | (imported) | Claim attach/dedup, `refresh_claim_summary`, creation-time classifier tags |
| `curiosity_scorer.py` | (imported) | 0-100 priority scoring — §Pipeline mechanics |
| `score_curiosities.py` | 10m | Multi-objective p_* scores + surprise bonus + transfer/shape haircuts — §Pipeline mechanics |
| `portfolio_allocator.py` | 10m | 4-lane budget; weights in `portfolio_state.json` |
| `generation_throttle.py` | (imported) | 250/h generation cap |
| `sync_curiosity_views.py` | 5m | Queue materialization, front reservations, auto-retire/auto-resolve — §Pipeline mechanics |
| `sync_sqlite_to_state.py` | 10m | General SQLite → self_state.json sync |
| `resolve_curiosities.py` | 15m | Jaccard (≥ 0.30) resolution against recent experiments; skips retest provenance — §Retest & boundary lanes |
| `reconcile_retest_credits.py` | 15m (:08) | Retest-credit safety net — §Retest & boundary lanes |
| `transfer_tracking.py` | (imported) | Three-stage transfer lifecycle — §Transfer & Abstraction |
| `replication_tracker.py` | 15m | Replication rates + disagreements; refreshes `replication_state.json` every run |
| `calibration_loop.sh` | 15m | Champion/challenger calibration training, 50x benchmark weighting — §Confidence Calibration |
| `calibration_test.py` | 15m | Calibration scoreboard (routing experiment) |
| `calibration_audit.py` | 30m | Calibration vs external ground truth — §Confidence Calibration |
| `backfill_calibration_mv.py` | (after promotion) | Whole-table calibrated_confidence recompute (claim_evidence join) |
| `inject_probe.py` | (from external-probe) | Inject benchmark questions at priority 4 |
| `dump_domain_accuracy.py` | (from calibration_loop) | Per-domain BENCH3 accuracy → `domain_accuracy.json` |
| `reclassify_domains.py` | 15m | Heal empty-domain rows (worker_results inherit their experiment's classified domain; step 0) + reclassify uncategorized experiments |
| `domain_taxonomy_maintenance.py` | 15m | Domain merge + confidence update |
| `domain_merge_sync.py` | 10m | Auto-merge fragmented domains; extend embedding centroids |
| `domain_taxonomy_merge.py` | (CLI, on demand) | Canonical-resolution merge of fragmented domain names (the bulk variant behind the cron mergers) |
| `auto_classify_uncategorized.py` | (CLI, on demand) | Keyword-classify experiments with empty/NULL/uncategorized domain (backfill + spot use) |
| `domain_health_cache.py` | 15m | Per-domain persistence/influence scores for the scorer |
| `domain_steady_state.py` | 30m | Daily domain governance metrics snapshot |
| `novelty_retrain.py` | 15m | Retrain the novelty predictor as curiosities resolve (thresholded) |
| `experiment_rag.py` | (library + CLI) | Permanent FAISS embedding index over experiments (GPU Qwen3-Embedding-0.6B, 1024-d, incremental); powers the knowledge feed + dedup; fails open. `index` / `serve` / `status` CLI |
| `rag_index_guard.py` | 15m | Keep the RAG index fresh |
| `rag_quality_check.py` | 10m | RAG query quality check |
| `claim_skill_consistency_check.py` | 15m | Verify claim-skill mapping |
| `artifact_maintenance.py` | 15m | Artifact status normalization (6h scope) |
| `artifact_preserve.py` | (library) | Worker-alive evidence preservation → `~/.hermes/artifacts/<task_id>/` |
| `queue_audit.py` | 30m | Flag items stuck in the queue > N cycles |
| `queue_curator.py` | (Director toolkit) | Mark curiosities resolved once answered by N+ completed experiments; `--dry-run` preview |
| `queue_composition_monitor.py` | 10m | Synthesis-dominance check |
| `queue_entropy_monitor.py` | 30m | Shannon entropy of thread distribution; inject diversity if H < 2.0 |
| `task_janitor_v2.py` | 10m | Stale-task cleanup; spawned-priority normalization (incl. the decomposition-child `from_decompose_of` → root-priority heal — §Pipeline mechanics); orphaned-assignee normalization (non-installed assignee → `default`); **reblock abandon-guard** — a card blocked ≥ `MAX_BLOCKED_EVENTS` (3) times is ABANDONED not re-queued; the count reads `blocked` + reaper `gave_up` events (counting only `blocked` had let reaper-blocked cards re-queue forever, dozens of cycles each). ABANDON is genuinely terminal: `prometheus_db.abandon_task` kills the worker then makes ONE direct kanban write — `status='archived'` (guarded to non-terminal states) + a `task_events(kind='archived')` audit row — no CLI subprocesses (the old path ran `hermes kanban unblock`, i.e. every "abandon" actually REQUEUED the card into an endless respawn loop, and its 10s-timeout CLI calls killed whole janitor runs under load). **Footgun:** `kill_worker_process` kills via `pgrep -f <task_id>` — any process whose cmdline mentions a task id dies with the worker; never put task ids on a command line near it |
| `workspace_gc_run.py` | 15m | Clean old workspaces (> 30min completed) |
| `selfref_watchdog.py` | 15m | Self-referential bias (SR vs external success-rate gap); floors counted as 0.0, genuine NULLs excluded |
| `source_epistemic_monitor_run.py` | 30m | Per-generator quality signals: calibration pathologies (ALERTS) vs under/over-confidence (info); replication pathologies vs deliberate probes. Silent unless genuine pathology |
| `evidence_integrity_monitor.py` | 30m | Duplicate key_findings, REPLICATED without retests, broken evidence chains, is_cross_domain drift, tfidf re-entry → `evidence_integrity_report.json` |
| `bench3_cross_domain_remeasurement.py` | 6h | Stratified BENCH3 accuracy in- vs cross-domain → `bench3_cross_domain_status.json`; alert if gap > 5pp |
| `db_reconciliation_monitor.py` | 30m | Cross-DB invariants (lost experiments, unreached completions, wrong-DB results, untagged empties, NULL ids, text timestamps); counts `synthesis_outputs.synthesis_task_id` as synthesis-task coverage while separately alerting stale unapplied synthesis rows; silent when healthy → `db_reconciliation_report.json` |
| `prometheus_db_health_check.py` | 5m | sqlite3 integrity check on prometheus.db |
| `script_drift_sentinel.py` | 15m | Deployed-code drift: sha256 every repo-clone `scripts/*.py\|*.sh` against its live twin under `~/.hermes/scripts/`; ALERT + exit 1 per drifted file, repo-only files are info in `script_drift_report.json`, fail-open without a readable repo. Guards the worker-clobber class: an A1 worker once rewrote `write_worker_result.py` in place after a routine argparse error, silently dropping verification + calibration for 2.5h. Live tunes must be synced into the repo to stay silent |
| `health_signal.py` | 2m | Writes `system_health.json` (coverage, hourly rates, queue flow, cost estimate) — the metrics file the Director (`director_diagnostic`/`full_check`/`cycle_report`), `post_deploy_monitor`, and `write_leak_watchdog` read. Freshness is a **self-heartbeat** (`now − own last write`, threshold 300s): if this job stops, the file self-latches `conservative_mode` and the steering layer runs on stale metrics — which is exactly what happened for 29h when the script existed with consumers but NO schedule (fixed 2026-07-11; the lesson: a live consumer does not prove a live producer). Silent-when-healthy; prints only at a tty, under `HEALTH_VERBOSE`, or on an actionable alert |
| `backup_prometheus_db.py` | 15m | Online-backup API → quick_check → atomic promote; keeps 10 (`MAX_BACKUPS`, 2.5h rolling — was 28/7h; the 1.8 GB snapshots dominated `backups/`) → `backups/prometheus-db/` |
| `backup_kanban_db.py` | 30m | Same pattern for kanban.db |
| `dashboard_watchdog.py` | 10m | Matched-pair watchdog: :8889 down → restart `prometheus-dashboard.service` (the unit that owns it); :8888 (v1, detached) down → setsid relaunch. (v2 fix: the old version probed :8888 but bounced the :8889 unit — could never revive v1, bounced healthy v2) |
| `topology_refresh.py` | 60m (`25-59/60`) | Hourly producer for `topology_full_export.json` + both topology plates (export → 2D → 3D, ~7s total). The export is live infrastructure (outcome_routing, auto_tune, calibration_test, topology_health_collector, the :8889 dashboard read it) and previously had NO producer — it drifted 13h+ stale between manual runs |
| `resource_watchdog.py` | 10m | Silent-when-healthy checks of critical files |
| `gpu_sklearn_hook_guard.py` | 15m (:08) | Restores `gpu_sklearn_hook.pth` in both venvs + removes the stale `.pth.disabled` twin (a venv rebuild wipes the loose `.pth`) — §GPU Sklearn Acceleration |
| `topology_health_collector.py` | 10m | Per-domain topology metrics, derivatives, alerts |
| `build_topology_export.py` → `topology_report.py` → `update_topology_html.py` / `update_topology_3d.py` | hourly via `topology_refresh.py` | Topology pipeline. The export JSON is LIVE INFRASTRUCTURE (outcome_routing, auto_tune, calibration_test, topology_health_collector all read it) — schema is **additive-only**. Deterministic per DB state; weighted PageRank; per-domain claim tiers, momentum, attack records (NARROWED neutral). Renderers are self-contained offline HTML (no CDN/webfonts/JS deps) → `docs/prometheus-topology.html` + `docs/prometheus-topology-3d.html` + `~` mirrors, cross-linked. Build ~1 min |
| `auto_tune.py` | 15m | Self-tuning controller: outcome-aware routing weights, novelty, trust, injection rate |
| `outcome_routing.py` | 5m | Per-edge routing scores (flow + outcome + novelty + trust) |
| `inject_opportunities.py` | 5m | Four-tier injection by transfer success rate (T1 ≥ 80% → T4 refutation_boost < 50%; per-tier minimum quotas + saturation feedback, batch cap 50). T3/T4 deliberately probe ambiguous/likely-negative space — their low calibrated confidence is correct. **Contested-pair gate** (all tiers): skips pairs in `contested_transfer_pairs` — ill-posed questions re-run ≥ 20x with a near-coin-flip verdict spread (`coinflip >= 0.60`); these never converge by construction (e.g. ml→motor_learning), so re-testing is noise. Convergent high-N and all low-N pairs are NOT gated |
| `refresh_contested_pairs.py` | 30m | Recompute + full-replace `contested_transfer_pairs`; helper `transfer_convergence.py` |
| `cross_domain_inject.py` | 5m | Confirmed mechanisms from saturated domains × under-explored frontier domains → `[TRANSFER]` tasks at p1; flock-guarded; worker assignment from `worker_config` |
| `compression_synthesis_run.py` | 30m | §Compression / Structural Synthesis |
| `confirmation_rate_monitor.py` | 60m | Recent confirmation rate (last 100 results) |
| `answer_consistency_adjudicator.py` | 15m (:09) | Answer-level consistency of supports for promotion-band claims (LLM); incompatible answers → DISPUTED; fingerprint-deduped in `answer_adjudications` — §Spurious-Agreement Gate |
| `circularity_critic.py` | 30m | Flags self-fulfilling constructions from preserved code + findings → `circular_construction=1`; conservative (quoted evidence + confidence ≥ 0.7) |
| `method_code_alignment_critic.py` | 30m | Flags claims whose finding's METHOD ≠ the preserved CODE (ScientistOne CoE I4) → `method_code_mismatch=1`; majority-vote over 3 cross-family judges, per-experiment, head+tail code excerpt |
| `adversarial_replication_enqueuer.py` | 15m | Break-lane attack enqueuer — §Adversarial attacks & claim scoping |
| `dispute_arbitration_enqueuer.py` | 15m | Decisive-experiment arbitration enqueuer — §Dispute arbitration |
| `novelty_audit.py` | EST 2h + REPL 15m | Literature audit + independent finder — §Literature & novelty lanes |
| `novelty_residue_injector.py` | 30m | Audit residues → `[LIT-RESIDUE]` questions — §Literature & novelty lanes |
| `novelty_contradiction_attacker.py` | 30m | CONTRADICTED audits → re-derivation attacks — §Literature & novelty lanes |
| `recall_audit.py` | 30m (`27-59/30`) | KNOWN-bin inverse audit, mechanical-only cron — §Literature & novelty lanes |
| `scholar_search.py` | (module + CLI) | Scholarly-index retrieval leg (Semantic Scholar + OpenAlex) — §Literature & novelty lanes |
| `symbolic_verifier.py` | 15m | Extracts "<decimal> = <closed form>" from promoted findings, evaluates with a safe AST evaluator (no `eval`) at stated precision. VERIFIED → posterior-neutral row in `analytic_verifications` (NOT claim_evidence: the orphan sweep deletes `worker_result_id IS NULL` rows, and an incidental identity is not support). MISMATCH → report-only by default (`--enqueue-mismatch` gates the recompute-enqueue). Constant provenance (π, e, Feigenbaum, `mpmath.identify` under `~/vllm-env`) → report-only leads. Extraction rejects operator-led fragments, bare parens/negatives, and multiplier-suffix decimals (a glued `x`/`×` marks a rate, not an operand) |
| `prior_override_report.py` | 1h | Scores preregistrations (signed direction vs OBSERVED sign, framing-independent — never the binary verdict); confirmation rate reported as an UPPER bound, overrides are the trustworthy signal. Read-only → `prior_override_status.json` |
| `mechanism_calibration.py` | 1h | Prior-confirmation by mechanism SHAPE (regex classifier: THRESHOLD / SCALING / TRANSFER / PHASE / CONSERVATION / OPTIMUM / EQUIVALENCE / MONOTONIC / OTHER), same sign-vs-sign scoring. MONOTONIC measures as the most over-trusted shape. Read-only → `mechanism_calibration.json` (consumed by score_curiosities) |
| `meta_claim_classifier.py` / `empirical_fact_classifier.py` | (library + backfill CLI) | `is_meta` / `is_empirical_fact` tagging at claim creation — §Data conventions & quirks |
| `meta_claim_prober.py` | 1h | Makes mature meta-claims (self-beliefs about transferable mechanisms) earn their tier — they are exempt from the normal falsification lanes, so this risks them deliberately: picks a well-supported transfer/mechanism-shaped meta-claim and an unseen target domain (breadth-first, up to `MAX_TARGETS_PER_CLAIM=3` distinct domains per claim — n=1 is coin-flip noise), enqueues a GENERALIZATION attack via `--claim` (bypasses the is_meta/tier filters), PREREGISTERING a signed direction; the card re-embeds the claim's `HYPOTHESIS:` so the result hash-collides onto the same meta-claim while `ATTACK_OUTCOME` routes the normal trichotomy. Ledger `meta_transfer_predictions`; hourly `--reconcile --apply` reads resolved outcomes + signed pre/post directions → `~/.hermes/meta_transfer_calibration.json` (consumed by score_curiosities' transfer haircut). Measured: the system systematically overestimates its own generalizability — self-asserted transfers mostly do not hold cleanly in unseen domains |
| `discovery_routing.py` | (library, pure) | Route cascade + self-test — §Discovery shelf |
| `discovery_spotlight.py` | 1h | Robustness scoring + hardening of discovery-route claims — §Discovery shelf |
| `discovery_report.py` | 60m | Discoveries plate renderer — §Discovery shelf |
| `claim_similarity.py` | 15m | Near-duplicate promotion-band claim detector (Qwen3-Embedding-0.6B, 1024-d) → `claim_near_duplicates` ledger; report-only, surfaced as Discoveries §4 — §Discovery shelf |
| `novelty_calibration.py` | (CLI) | Shelf self-audit (offline) + index-armed adjudication → `NOVELTY_CEILING` — §Discovery shelf |
| `world_grounding.py` | 30m | Toy-vs-world lane — §Discovery shelf |
| `independence_gate.py` | 10m | Independence measurement, self-arming haircut, clean-room enqueue — §Knowledge feed & independence |
| `prior_feed_stamp.py` | (library) | Durable per-task feed stamp + blind-lane suppression — §Knowledge feed & independence |
| `cross_domain_disconfirmation_gate.py` | 1h | The asymmetric cross-domain discount built as a GATE not a weight (§Evidence Integrity for the measured asymmetry): finds claims demoted *purely* by out-of-domain evidence (DISPUTED with cross-domain refutes and zero same-domain refutes) and enqueues an **in-domain** corroboration re-test via `--claim`. SURVIVED in-domain ⇒ the dispute doesn't reproduce where the claim lives (recovers via normal recompute); BROKEN ⇒ corroborated, stands. Ledger `xdomain_disconfirm_checks`; never writes status/posterior |
| `tmp_stray_janitor.py` | 30m | Sweeps aged scratch (/tmp gpu_sklearn_*/browser profiles > 2h, shell stubs > 24h, owner-checked); worker-output strays in `~` and `~/.hermes` root MOVED (never deleted) to `archive/stray-sweeps/`; leaves `experiments/` and `scripts/` alone. Workers spawn with `TMPDIR=<workspace>` so per-task scratch dies with the workspace. Also sweeps **0-byte ghost DBs** — empty `*.db`/`*.sqlite` (+ `SELECT …`/`prometheus_db=` filename junk) an agent mints by running `sqlite3 <bare-name>`/`connect('<name>.db')` from the `~/.hermes` cwd (LLM guesses a name → sqlite auto-creates the empty file; the `knowledge_clames.db` typo class) — from the `~/.hermes` root, the `$HOME` root, and the cwd-accident dirs (`data/ prometheus/ kanban/ prometheus_worker_results/ director-workspace/`), age > 2h, guarded by size≠0 + a live-DB allowlist so a real DB is never touched |
| `hf_cache_janitor.py` | 6h | LRU-evicts HF models/datasets to keep `~/.cache/huggingface` under 100 GiB (workers re-download on demand); silent under budget |
| `self_repair_scanner.py` | 30m | Check recent experiment findings for actionable problems |
| `auto_expand_verification_db.py` | 15m | Feed the working-memory daemon experiment-derived concepts/associations; watermark-tracked |
| `wm_focus_watchdog.py` | 1m | Update WM daemon focus from the most recently heartbeated running task |
| `inspector` | 120m (LLM) | Structural health monitoring — runs watchdog then analyzes output |
| `external-probe` | 60m (LLM) | Web search → fresh true/false benchmark questions |
| `health_snapshot.py` | on demand | Operator health board — see below |
| `revert_drain_surge.py` | on demand | Restores all steady-state dial values after a drain — Drain mode, §Pipeline mechanics |

**One-shots** (already ran; stories in the changelog — do **NOT** cron): `link_transfers.py`, `backfill_transfer_tracking.py`, `corroborate_backfill.py`, `scholar_recheck.py`, `flood_retest_queue_20260703.py`, `backfill_boundary_scopes_20260704.py`, `backfill_claim_summary.py`, `backfill_reopen_poisoned_retests_20260702.py`, `fix_evidence_integrity.py`.

**Retired** (still on disk — do NOT run): `ground_all_claims.py` — it never actually searched the literature and wrongly decayed posterior; superseded by `novelty_audit.py`, which keeps grounding strictly metadata.

**Operator board — `health_snapshot.py`** (`python3 ~/.hermes/scripts/health_snapshot.py`, or `--json`): one screen — throughput, confirm/refute mix, transfer-survival rate, mechanism accumulation, SA honesty (promotion-band false consensus, with all-tiers as context), a **GATES** section (retest credit yield, retest-gate backlog, arbitration pending/resolved, attack survival rate, the **per-claim scoping-convergence triple** — converged (latest post-scope outcome survived) / mapping (< 3 narrows) / orbiting (≥ 3 narrows, 0 survivals); green when ≥ 50% converged ∧ ≤ 10% orbiting; the old cumulative held-vs-narrowed attack totals were a conflation of orbit debris, material cross-domain boundaries, and active mapping, and are kept only as context — independence armed/haircut — `Y / 0` = armed but toothless, stamps not joining shelf supports — db lock failures), depth shown WITH transfer context (depth is a thermometer, not the goal; transfer survival is the product), dispatch-pipeline state, and a one-line verdict. Three standing GATES tripwires: **retest-credit yield** (healthy 85-100%; < 85% = block 1g leaking / reconciler stalled — but first check for a new quote-collider generation: a real retest carries its `[CANDIDATE-RETEST]`/`[RETEST-GATE]` tag in the title HEAD, enforced as `INSTR(title, tag) BETWEEN 1 AND 40`; follow-up/attack questions that QUOTE a retest title embed the tag deep in a quoted string, write ordinary worker_results, and can never credit — two collider generations have already tripped this gauge falsely), **retest-dup completions** (healthy ≈ 0; > 10% = task-mint dedup regressed), **db-lock failures** (healthy ≈ 0/day; > 5 = a long-transaction writer is starving the cron writers — §Database Concurrency). Plus the drain-complete tripwire (Drain mode, §Pipeline mechanics).

### Background Services

Six systemd services provide persistent infrastructure outside the cron pipeline:

| Service | Port | What it does |
|---|---|---|
| `prometheus-dashboard` (user unit) | 8889 | The v2/v3 dashboard (`experiments/prometheus_dashboard_v2.py`, turquoise-plate rewrite), OOM-hardening drop-in. The system-scope `prometheus-dashboard-v2.service` conflicts for the same port and must stay **disabled** |
| (detached process) | 8888 | Dashboard v1 (`~/prometheus_dashboard.py`); `dashboard-watchdog` (10m) checks BOTH ports — relaunches this one detached, restarts the systemd unit for :8889 |
| `wm-daemon.service` | 19876 | Working memory daemon — persistent associative concept graph (concepts, weighted associations, focus); fed by `auto_expand_verification_db.py` + `wm_focus_watchdog.py`; 5-min idle auto-shutdown |
| `gpu-embed.service` | 9150 | Qwen3-Embedding-0.6B (1024-d), **4 parallel slots** (`--parallel 4`) embedding server for RAG semantic search. **The launcher is the SYSTEM unit `/etc/systemd/system/gpu-embed.service`** (sudo to edit its ExecStart), which owns port 9150 via `ExecStartPre fuser -k`; `hermes-rag.service`/`experiment_rag.py serve` was a redundant second manager it stomped — **stopped + disabled 2026-07-06**, so gpu-embed is now the sole manager. Server runs detached (`setsid`) — survives `systemctl restart`, so `kill` the llama-server PID directly to cycle new flags. Asymmetric: docs embedded raw, queries `Instruct:…\nQuery:` prefixed) |
| `agents-a1-fp4.service` | 8001 | **Agents-A1** free local reasoning worker (Qwen3.5-MoE hybrid Gated-DeltaNet, **TextOnly-FP4** weights — `~/models/Agents-A1-TextOnly-FP4`; the sibling `Agents-A1-NVFP4` dir is the superseded quant) on the 5090 via vLLM (`--served-model-name agents-a1`). **6 seqs × 96K** (`--max-num-seqs 6 --max-model-len 98304`, util **0.82** → KV pool ~504K tok / 5.13× concurrency; the model is natively 256K, capped for VRAM). Util is 0.82-not-0.86 because the card is SHARED: each fleet worker touching torch opens a ~650–714 MiB CUDA context, several run concurrently (4 observed), and at 0.86/0.85 that intruder load squeezed A1's runtime slack to ~13–27 MiB free → EngineCore `torch.OutOfMemoryError` (two deaths on 2026-07-08). Unit is `Restart=always` + `RestartSec=45` + `StartLimitIntervalSec=0` (in `[Unit]`) — vLLM's APIServer exits CLEANLY (rc=0) on EngineDeadError, so `on-failure` never fired and every OOM death used to strand :8001 down until a manual start; now a death or a can't-allocate boot just retries until the intruders vacate. `systemctl stop` ORPHANS the EngineCore (keeps holding VRAM) — `kill -9` the EngineCore PID to actually cycle flags (§changelog 2026-07-07). Serves the transfer/exp worker lane + the interactive `a1` profile. |
| `agents-a1-router.service` | — | **Local-first router** (`scripts/a1_router.py`, 10s tick): stamps ready `[TRANSFER]`/`exp_<digit>` cards with `model_override='agents-a1'` (cap 8; a deny-list keeps adversarial/arbitration/boundary/etc. off A1) and clears the A1 pin on repeated failure. **Opt-in — kill switch `touch ~/.hermes/a1_router.OFF`** pauses stamping. Whether a stamped worker actually reaches A1 (vs silently falling back to mimo) depends on the `kanban_db.py` arg-order + `chat_completions.py` clamp fork mods (§Fork modifications). |

**Fleet resource governance (cgroups v2).** The worker fleet (`hermes-gateway.service`) is CPU-capped via systemd `CPUQuota` (persistent drop-in `~/.config/systemd/user/hermes-gateway.service.d/90-cpuquota.conf`, **standing 82% = 26/32 cores**) so the ~20 concurrent experiments — single adversarial/arbitration runs use 6–14 cores each — can't saturate all cores and starve the embed server, RAG reindex, and crons (uncapped, the box hit load 85 on 32 cores). Retune live, no restart: `systemctl --user set-property hermes-gateway.service CPUQuota=<N×32>%` (2624%=82%, 1600%=50%). The embed server (`gpu-embed.service`), RAG, and dashboards live in SEPARATE cgroups, so the cap frees guaranteed headroom for them. `nice` alone is insufficient — a niced job still starves under load; the hard cap is the fix.

**Generated editorial pages** (static HTML in `docs/` + `~` mirror, turquoise-plate contract, no server): `prometheus-topology.html` (2-D routing report), `prometheus-topology-3d.html` (3-D companion), `prometheus-discoveries.html` (the discovery shelf, refreshed hourly). All three cross-link.

### GPU Sklearn Acceleration

Transparent GPU acceleration for sklearn via a persistent daemon:

- **Hook:** `gpu_sklearn_hook.pth` intercepts sklearn imports and redirects to GPU-accelerated versions (LogisticRegression, PCA, StandardScaler, KNeighborsClassifier, SelectKBest). **Active in BOTH venvs as of 2026-07-05**: the vllm-env copy (daemon-side / `gpu_run`) and the **hermes-agent (worker) venv** copy — workers run in the latter, which carries torch 2.11+cu130, so the old "no torch there, keep it `.pth.disabled`" rationale was wrong and had left worker sklearn silently CPU-only. `gpu_sklearn_hook_guard.py` (§Key Scripts) keeps both enabled. Live-verify: `~/.hermes/hermes-agent/venv/bin/python -c "from sklearn.linear_model import LogisticRegression; print(LogisticRegression.__module__)"` → `gpu_sklearn._core`.
- **Daemon:** `_gpu_daemon.py` runs in the vllm-env (PyTorch 2.11+cu130, Blackwell sm_120), loads CUDA once, accepts requests via Unix socket (`/tmp/gpu_sklearn_daemon.sock`), 5-min idle auto-shutdown. **Singleton-enforced** (2026-07-05): `_client.py` serializes daemon startup under `flock` (`/tmp/gpu_sklearn_daemon.lock`) and probes liveness by PID, not a ping that false-negatives on a busy daemon. Without this, ~20 concurrent workers each spawned their own daemon and stormed the GPU with dozens of ~650 MB CUDA contexts — the real cause of the recurring "hook disabled" saga.
- **Client:** `_client.py` manages daemon lifecycle (auto-start), sends requests, cleans temp files.
- **Performance:** LR fit 2.6x, PCA 1.4x at 10K samples (warm daemon); predict ~1x (data-bound).
- **Fallback:** graceful CPU sklearn on any error. Workers need zero code changes.

Scripts: `~/.hermes/scripts/gpu_sklearn/{_core.py, _client.py, _gpu_daemon.py}`.
Active hook (BOTH venvs): `~/vllm-env/lib/python3.14/site-packages/gpu_sklearn_hook.pth` **and** `~/.hermes/hermes-agent/venv/lib/python3.14/site-packages/gpu_sklearn_hook.pth` (a `pip install --force` / venv rebuild wipes them — part of the fork's patch stack, restore copy in `~/.hermes/fork-patches/`; the `gpu-sklearn-hook-guard` cron re-restores both every 15 min).

**`~/vllm-env` is load-bearing — never delete or rename it.** It is the box's only torch+CUDA runtime (the system python has no torch): the gpu_sklearn daemon runs in it, and so does `gpu_run` (`/usr/local/bin/gpu_run`), the persistent-daemon CLI that task cards point workers at for large-model inference. `gpu_run` detects its runtime by string-matching "vllm-env" in `sys.executable`, so even a rename breaks GPU compute; workers also pip-install experiment dependencies into it on demand. The name is a fossil of its vLLM-serving origin — judge it by its dependents, not its name. Same class of trap: **`~/.hermes/gpu_scheduler.py` is the ONLY copy** — `gpu_run` and `dataset_confidence_scorer` import it via `sys.path.insert(~/.hermes)`; it looks like a loose root stray but is load-bearing (a tidy sweep once atticked it and broke gpu_run).

### Fork modifications to the hermes-agent base (post-defork: PR bucket only)

**The de-fork migration EXECUTED 2026-07-08** (plan: `docs/defork-plan.md`; execution + verification: changelog). **Current base (2026-07-10): upstream tip `b8880f124`** — branch `prometheus-fork` = origin/main + **the 15 open-PR branches cherry-picked** (#61221-22, #61224-28, #61230-34, #61674, #62121-22 — each carrying its sweeper-review follow-up commit) + **2 deployment-residue commits** (pyproject `<3.15`/SKILL doc; A1 provider routing — re-appending `-m agents-a1 --provider agents-a1` after the `chat` token, which re-breaks on every base update). Rollback tags: `pre-defork-cutover`, `pre-update-20260708`, `pre-update-20260710`. Two former PR commits are now native upstream and dropped from the bucket (#61229 goal_max_turns, #61235 arg-order — closed as implemented-on-main). The base tree carries **only the PR bucket + residue**; all site policy lives in an update-proof layer outside the repo. Update procedure: cherry-pick the PR branches onto new tip + residue commit — NOT a history replay; diff every cherry-pick against tip treating net deletions as suspect (a stale-base patch once silently reverted an upstream leak fix); and **always verify the A1 lane by :8001 POST counts after an update** — the router's `used=N` stamp is not proof of connection (the lane once went dark for ~an hour post-update while every stamp looked healthy).

**The update-proof layer** (survives any `git pull`/reinstall):
- **3 user plugins** in `~/.hermes/plugins/`: `prometheus-guard` (`pre_tool_call`: tool-hallucination + uncertainty-deferral guards + the worker-result-written completion gate), `prometheus-prompt-policy` (import-time module-attribute rebind of MEMORY_GUIDANCE in prompt_builder + system_prompt, `_MEMORY_REVIEW_PROMPT`, and a guardrail splice into `_COMBINED_REVIEW_PROMPT` — the old "prompts can't move to a plugin" claim was wrong: `pre_llm_call` can't touch the system prompt, but attribute rebinding at `discover_plugins()` time can, since it runs in every process before first prompt build), `prometheus-runtime-tuning` (cron `MIN_GRACE` wrapped `max(300, orig)`, kanban redaction rebound to identity). All sentinel-guarded: on upstream drift they log `PATCH FAILED`, write a marker file, and fail OPEN (stock behavior).
- **CRITICAL WIRING RULE:** worker profiles are SELF-CONTAINED (`HERMES_HOME` switch — profile config never merges with main `config.yaml`). Every plugin must be BOTH symlinked into `profiles/<name>/plugins/` AND listed in that profile's `plugins.enabled`. Currently wired: main + `a1` + `interactive`, all three plugins. A plugin enabled only in main config silently doesn't exist for workers.
- **Sidecar cron** `fleet_policy_stamper.py` (`7-59/5`): stamps `goal_mode=1/goal_max_turns=6` and the `kanban-worker` skill on ready tasks (upstream's own per-task columns — the fleet-wide goal loop no longer needs spawn-code), reassigns over-cap/phantom assignees to `default`, backfills model provenance. The enqueuers (`task_refiller.py`, `batch_create_tasks.py`) also write those columns directly at INSERT, so the stamper is backfill, not the primary path.
- **Config knobs**: `kanban.auto_subscribe_on_create: false`, `skills.external_dirs: [~/.hermes/skills]` (main + both worker profiles).
- **12 staged upstream patches** in `~/.hermes/fork-patches/upstream-prs/` (README = inventory + bases + submission steps; no `gh` on this box). Two behaviors were deliberately DROPPED from base at cutover and return only when their PRs merge: `process_registry` venv-env injection for background spawns (0011) and the KANBAN_GUIDANCE anti-false-block wording (0012 — the guard plugin covers the false-block failure mode at the tool layer).

**Updating the base** is still a maintenance-window job, but conflict surface is now small: `git fetch origin` → `git rebase origin/main` on `prometheus-fork` → `pip install -e . --no-deps` (venv already carries deps; the py3.14 box can't build upstream's Rust transitives, which is why `pyproject.toml` raises `requires-python` to `<3.15`) → `npm install` (never hand-merge `package-lock.json`) → restart gateway/dashboard/health/rag services. After any update: check for `PATCH_FAILED` markers in `~/.hermes/plugins/*/` — a marker means upstream drifted under a sentinel and that patch is inert until refreshed. Drop any base commit whose upstream PR has merged.

| Base file (patch №) | What the base still carries and why |
|---|---|
| `hermes_cli/kanban_db.py` (0006) | **Six features on the upstream-tip base** (upstream lifecycle plugin hooks, claim-lock reclaim guards, and the dispatch-tick lock are re-adopted): init **byte-lock serialization** (~20 writers); **A1 spawn routing** — `model_override='agents-a1'` emits `--provider custom:agents-a1 -m agents-a1` AFTER the `chat` token (top-level placement lets the subparser default clobber them → worker silently runs mimo); **bounded reaper retry** for clean-exit protocol violations (limit 3, ~96% recover); **decompose children inherit ROOT priority** (p0-starvation fix); **TMPDIR=workspace** temp routing; **torn-extend invariant tolerated in WAL mode**. Gone from base: per-task skill injection and the fleet-wide goal-mode spawn default — both ride upstream's own `skills`/`goal_mode`/`goal_max_turns` task columns now, written by the enqueuers + `fleet_policy_stamper`. |
| `tools/kanban_tools.py` (0007) | **Assignee coercion** in `kanban_create` (missing/phantom assignee → `default`; schema field optional) + `skills` schema description tweak. The in-file guard copies are DELETED — `prometheus-guard` is the only guard layer now. |
| `agent/conversation_loop.py` (0008) | Overflow handling: compression-attempts cap 3→30, output-cap retry margin 64→512, **overflow-spiral guard** (one non-converging `max_tokens` reduction → route to real input compression). |
| `agent/chat_completion_helpers.py` (0008) | Feeds `context_length` + `estimated_input_tokens` into profile-path `build_kwargs` — inputs for the clamp below. |
| `agent/transports/chat_completions.py` (0008) | **A1 output-cap clamp**: custom-profile `max_tokens` capped to `ctx − est − max(512, est//25)` — a strict endpoint (vLLM) otherwise 400s every call → worker never completes → auto-block cascade. |
| `cli.py` (0009) | Goal loop honors dispatcher-passed `HERMES_KANBAN_GOAL_MAX_TURNS` (6-turn budget, not the 20-turn default). |
| `cron/scheduler.py` (0001) | Splits a cron `script` field into path + argv (shlex) so args-bearing script jobs dispatch. |
| `tools/checkpoint_manager.py` (0002/0010) | gc `--prune=2.hours.ago` grace window (prune-now under ~20 sessions → dangling refs, rc=128 hand-repair) + concurrent-gc lock rejection demoted to debug. |
| `tools/daemon_pool.py` (0003) | py3.14 `ThreadPoolExecutor._worker` signature compat (without it the dispatcher spawns 0 workers fleet-wide). |
| `gateway/status.py` (0004) | Liveness check tolerates setproctitle-stripped argv (dashboard falsely reported gateway OFFLINE). |
| `hermes_cli/env_loader.py` (0005) | dotenv reload retries transient `KeyError` (races the in-gateway decomposer mutating `os.environ`). |
| `pyproject.toml` (deployment-local, not PR'd) | `requires-python <3.15` — box runs 3.14.4; installs use `--no-deps`. |
| `skills/…/hermes-agent/SKILL.md` (docs) | Documents `delegate_task`'s `toolsets` param on the worker card. |
| tests (2 guards + 4 reshaped) | Guards: A1 arg-order (`test_kanban_goal_mode.py`), output-cap clamp (`test_e2e_wiring.py`). Reshaped to upstream semantics at de-fork: `test_kanban_core_functionality/​_db/​_goal_mode/​_init_lock_bounded.py`. |

Fully de-forked (byte-identical to upstream): `agent/prompt_builder.py`, `agent/background_review.py` (prompt policy → plugin), `tools/process_registry.py` (venv-env injection dropped, PR 0011), `tools/skills_tool.py` (upstream's own `_skills_dir()` + `skills.external_dirs` config covers it), `cron/jobs.py` (MIN_GRACE → runtime-tuning plugin). Legacy backstops (`fork-patch-capture` cron, `prometheus-fork-latest.patch`, `MANIFEST.md`) still run but now capture a much smaller delta.

The one base customization documented elsewhere — the `gpu_sklearn_hook.pth` (§GPU Sklearn Acceleration) — is the same class of risk: a `.pth` in the venv that a `pip install --force` / venv rebuild also wipes. Treat the git modifications + the `.pth` hook together as **the fork's required patch stack**.

### Benchmark Schema

- `curiosities.benchmark_id TEXT` — tags benchmark questions (BENCH-T-NNN, BENCH-F-NNN, BENCH2-T-NNN, BENCH2-F-NNN, BENCH3-T-NNN, BENCH3-F-NNN)
- `curiosities.known_answer TEXT` — "true" or "false"
- `worker_results.benchmark_id TEXT` — propagated from parent curiosity
- `experiments.benchmark_id TEXT` — propagated from parent curiosity

---

*Full history: `docs/architecture-changelog.md`.*
