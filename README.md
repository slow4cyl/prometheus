# Prometheus

**An autonomous research system that runs 24/7 on one workstation — and is built to distrust itself.**

> **No relation to [Prometheus monitoring](https://prometheus.io)** — this project shares only the name.

## Prerequisite

Prometheus is a research layer, not a standalone agent runtime. It **requires
[hermes-agent](https://github.com/NousResearch/hermes-agent)** — the open-source
agent substrate that provides the gateway, kanban dispatcher, worker spawning,
cron ticker, and plugin system this repo builds on. Install it first
(see `SETUP.md`), then lay Prometheus on top.

Prometheus turns a single Linux box with one GPU into a self-directing research fleet: it generates its own questions, dispatches LLM workers to run real experiments with preserved code, extracts claims with scoped confidence, and then spends a large fraction of its compute **attacking its own conclusions** — adversarial replication, cross-domain disconfirmation, novelty verification against the actual literature indexes, and calibration audits that measure how often the system's own confidence is wrong.

It is not a chatbot, not a demo loop, and not turnkey. It is a working reference deployment: ~90 scheduled jobs, ~100 orchestration scripts, two SQLite WAL databases, three fail-open runtime plugins, and a local vLLM worker fleet on a single RTX 5090 — running continuously and processing over 100,000 tasks to date.

---

## The core loop

```
 curiosities ──► scoring ──► task queue (priority lanes) ──► kanban dispatcher
      ▲                                                            │
      │                                              ┌─────────────┴─────────────┐
      │                                              ▼                           ▼
      │                                    local A1 workers            API-lane workers
      │                                  (vLLM on RTX 5090,          (burst / frontier
      │                                   free, 6×96K ctx)             capability)
      │                                              └─────────────┬─────────────┘
      │                                                            ▼
      │                                            experiments (code preserved,
      │                                             results → worker_results)
      │                                                            ▼
      │                                              claims (scoped confidence,
      │                                               claim_hash identity)
      │                                                            ▼
      │            ┌───────────────────────────────────────────────┴───────┐
      │            ▼                   ▼                   ▼               ▼
      │      adversarial         cross-domain         novelty vs      independence
      │      attack lane         disconfirmation      literature      & circularity
      │      (replication,       gate                 indexes         gates
      │       contradiction)                                              │
      │            └───────────────────┬───────────────────┬──────────────┘
      │                                ▼                   ▼
      │                      calibration loop        discovery spotlight
      │                      (meta-prober,           (the terminus: what
      │                       mechanism trust)        actually survived)
      └────────────────────────────────┘
        contradictions and calibration misses become new curiosities
```

Every stage is a real, inspectable script in `scripts/`, wired into the cron
ticker by `cron/jobs.json`. Nothing is a black box.

## What makes it different

**It measures its own epistemic failure modes.** The meta-prober tests whether
the system can predict which of its own claims transfer to new domains (the
reference deployment measured itself at 53% — barely better than chance — and
responded by hair-cutting transfer confidence). Mechanism-level calibration
found MONOTONIC-type claims were over-trusted at 67.6% and reweighted them.
Contradicted claims are not deleted; they are routed to an attack lane and
fought over.

**Confidence is scoped, capped, and adversarially earned.** Claims carry a
`claim_scopes` ledger. Attack cards target the *mapped scope*, not a
strawman. A confirmation from a correlated source is worth less than one from
an independent lane (`independence_gate.py`), a claim that merely restates its
own evidence gets caught (`circularity_critic.py`), and a "novel" finding must
survive a check against actual literature indexes — title/abstract/venue in
front of the model — because an LLM's recall of the literature is not the
literature (`novelty_audit.py`, `scholar_search.py`).

**The prose must match the code.** `method_code_alignment_critic.py` checks
that what a worker *says* it did matches the preserved experiment code —
closing the gap most autonomous-research systems leave open (numbers get
drift-checked; methods sections usually don't).

**Infrastructure is self-healing and update-proof.** The agent substrate
(hermes-agent) runs *stock* — every behavioral customization lives in three
sentinel-guarded plugins that detect upstream drift, log `PATCH_FAILED`, and
fail **open** to stock behavior rather than breaking silently. All generic
fixes are submitted upstream (14 PRs; carried as clean cherry-picks until
merged). Watchdogs watch the dashboards; a reconciliation monitor
cross-checks the two databases against each other; a self-repair scanner
files its own maintenance tasks.

## The two-layer architecture

| Layer | What it is | Where it lives |
|---|---|---|
| **Hermes** (substrate) | Gateway, kanban dispatcher, worker spawning, cron ticker, profiles | upstream [hermes-agent](https://github.com/NousResearch/hermes-agent) + `fork-patches/` |
| **Prometheus** (research app) | Everything in this repo: lanes, gates, critics, calibration, dashboards | `scripts/`, `cron/`, `plugins/`, `dashboard/` |

Two SQLite databases (WAL mode, ~20 concurrent writers):
- **kanban.db** — dispatch: tasks, claims, heartbeats, task_events audit trail
- **prometheus.db** — knowledge: experiments, worker_results, knowledge_claims,
  claim_evidence, claim_scopes, discovery_candidates, calibration ledgers
  (schema in `schema/prometheus.schema.sql` — structure only, no data)

## Hardware reference

The reference deployment runs everything on **one machine**: a consumer
workstation with a single RTX 5090 (32 GB). The local worker is a 30B-class
MoE served by vLLM in FP4 (~1,400 tok/s, 6 concurrent 96K-token contexts) — so the
bulk of fleet compute is **free and local**; metered API models are reserved
for burst lanes. None of this is required: any OpenAI-compatible endpoint
works as the worker lane (see `SETUP.md`).

## Repository layout

```
scripts/            ~100 orchestration scripts — the system itself
                    (task_refiller, lanes, gates, critics, calibration,
                     watchdogs, janitors, backups; gpu_sklearn/ GPU shim)
cron/jobs.json      ~90 job definitions: schedules + prompts + script wiring
plugins/            prometheus-guard        (worker guardrails + completion gate)
                    prometheus-prompt-policy (memory-policy prompt rebinds)
                    prometheus-runtime-tuning (scheduler grace, redaction policy)
                    — all sentinel-guarded, fail-open
config/             config.example.yaml + worker profile examples
systemd/            service units (gateway, dashboard, local model, router)
schema/             prometheus.db schema (empty-database bootstrap)
skills/             kanban-worker + prometheus-* skills workers load per task
dashboard/          single-file live dashboard (fleet, lanes, alerts)
fork-patches/       upstream PRs carried until merged (see its README)
docs/               architecture-map.md — the full system reference
                    defork-plan.md — how the substrate was made update-proof
SETUP.md            fresh-machine bootstrap guide
```

`docs/architecture-map.md` is the deep reference: every lane, gate, dial, and
gotcha, written to be sufficient to operate the system without the author.

## Honest limitations

- **Single-box, single-tenant.** No multi-node story; concurrency limits are
  tuned to one machine's SQLite and one GPU.
- **Not turnkey.** `SETUP.md` is a real bootstrap path, but constants
  (lane budgets, confidence caps, GPU memory dials) encode months of tuning
  to this hardware and workload. Expect to re-tune.
- **The substrate moves.** hermes-agent evolves quickly; the plugin sentinels
  fail open by design, and `fork-patches/upstream-prs/README.md` tracks what
  still needs to ride along.
- **Research output quality is bounded by the models you point it at.** The
  system's contribution is the *epistemic machinery* — generation, attack,
  calibration, and honest accounting — not any single model's intelligence.

## License & credit

**MIT** (see `LICENSE`). Use it, fork it, build on it — for anything. The one
ask is baked into the license: keep the copyright/permission notice, i.e.
**credit this project when you use it**. If Prometheus ends up in something
you publish or ship, an acknowledgment or a link back here is appreciated.

Prometheus builds on [hermes-agent](https://github.com/NousResearch/hermes-agent)
(MIT, Nous Research) — the substrate deserves its own credit.
