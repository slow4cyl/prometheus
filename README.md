# Prometheus

**An autonomous research system that runs 24/7 on one workstation — and is built to distrust itself.**

[![tests](https://github.com/slow4cyl/prometheus/actions/workflows/tests.yml/badge.svg)](https://github.com/slow4cyl/prometheus/actions/workflows/tests.yml)

> **No relation to [Prometheus monitoring](https://prometheus.io)** — this project shares only the name.

Prometheus turns a single Linux box with one GPU into a self-directing research fleet: it generates its own questions, dispatches LLM workers to run real experiments with preserved code, extracts claims with scoped confidence, and then spends a large fraction of its compute **attacking its own conclusions** — adversarial replication, cross-domain disconfirmation, novelty verification against the actual literature indexes, and calibration audits that measure how often the system's own confidence is wrong.

It is not a chatbot, not a demo loop, and not turnkey. It is a working reference deployment: ~90 scheduled jobs, ~100 orchestration scripts, two SQLite WAL databases, three fail-open runtime plugins, and a local vLLM worker fleet on a single RTX 5090 — running continuously — over 140,000 experiments across 110,000+ dispatched tasks as of July 2026. Built solo, from scratch, in about a month, on one consumer gaming PC — as a first project.

---

## The numbers it publishes about itself

The field's autonomous-research generators have outrun their validators; the
open problem is trust. So before the architecture, here is what this system
measured about **its own** trustworthiness — the numbers most projects don't
publish (snapshot: July 2026, reference deployment; the live values move on
its dashboard):

| It asked itself | Measured | Response |
|---|---|---|
| Can I predict which of my claims transfer to new domains? | **~58%** — barely above chance (53% on the first N=32 meta-probes) | transfer confidence hair-cut across the board |
| How much of my discovery shelf ever touched real-world data? | **~2%** — the shelf ran almost entirely on self-generated simulation code | built the toy-vs-world lane to re-test against external datasets |
| Do my simulation-validated claims survive real data? | **~69%** of verified re-tests hold (20/29) | the refusals are catalogued as first-class results, not buried |
| Which claim shapes do I over-trust? | MONOTONIC mechanisms, **~69%** over-trusted | reweighted at the calibration layer |

Every number above was produced by a scheduled job in this repo, against the
system's own knowledge base, and survives on the live dashboard. The honest
readings are the feature: a research system that can't tell you where it
fools itself can't be trusted where it doesn't.

**Receipts:** [`FINDINGS.md`](FINDINGS.md) is a labeled snapshot of actual
output — the six **reality's refusals** (simulation said yes, real data said
no), the verified world-holds, and the discovery shelf's top entries with
their honest caveats attached. The system's self-rendered pages are served
via GitHub Pages exactly as its hourly cron generated them:
[knowledge topology](https://slow4cyl.github.io/prometheus/prometheus-topology.html) ·
[topology 3-D](https://slow4cyl.github.io/prometheus/prometheus-topology-3d.html) ·
[discoveries board](https://slow4cyl.github.io/prometheus/prometheus-discoveries.html) ·
[architecture diagram](https://slow4cyl.github.io/prometheus/prometheus-architecture-diagram.html)

---

## Prerequisite

Prometheus is a research layer, not a standalone agent runtime. It **requires
[hermes-agent](https://github.com/NousResearch/hermes-agent)** — the open-source
agent substrate that provides the gateway, kanban dispatcher, worker spawning,
cron ticker, and plugin system this repo builds on. Install it first
(see `SETUP.md`), then lay Prometheus on top.

---

## The core loop

```
 curiosities ──► scoring ──► task queue (priority lanes) ──► kanban dispatcher
      ▲                                                            │
      │                                              ┌─────────────┴─────────────┐
      │                                              ▼                           ▼
      │                                    local A1 workers            API-lane workers
      │                                  (vLLM on RTX 5090,          (~14 concurrent slots,
      │                                   free, 6×96K ctx)          incl. model-pinned lanes)
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
reference deployment measured itself at ~58% over ~500 self-probes — barely
better than chance, 53% on the first N=32 — and responded by hair-cutting
transfer confidence). Mechanism-level calibration found MONOTONIC-type claims
were over-trusted (~69%) and reweighted them.
Contradicted claims are not deleted; they are routed to an attack lane and
fought over.

**It re-tests its simulations against the world.** Every other gate in the
system tests coherence — whether the system's runs agree with each other. The
toy-vs-world lane (`world_grounding.py`) is the only one that tests
*correspondence*: it takes claims validated in self-generated or simulated
settings and re-runs them against real external datasets, with the loader
code preserved and mechanically classified so a worker can't claim
"tested against real data" while running another simulation. In the
reference deployment, only **~69% of verified re-tests hold** (20/29) —
roughly three in ten simulation-validated findings are refused by reality.
Those refusals aren't buried; they're first-class results the lane records
and the dashboard displays. Most autonomous-research systems never ask this
question; the honest answer is the strongest argument for asking it.

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
workstation with a single RTX 5090 (32 GB), running **local and API workers
at the same time** as one ~20-slot pool. A router fills the local lane first —
a 30B-class MoE served by vLLM in FP4 (~1,400 tok/s, 6 concurrent 96K-token
contexts, free) — and everything past those 6 slots, plus the model-pinned
adversarial/cross-family lanes, runs through metered API models in parallel
(~14 slots). If the local endpoint is down, its share routes to the API too.
None of this is required: any OpenAI-compatible endpoint works as the worker
lane (see `SETUP.md`).

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
tests/              invariant tests (HERMES_HOME-isolated): domain policy,
                    maturity, confidence arithmetic, world-basis classifier,
                    schema bootstrap — `HERMES_HOME=$(mktemp -d) pytest tests/`
fork-patches/       upstream PRs carried until merged (see its README)
docs/               architecture-map.md — the full system reference
REFACTORING.md      tracked structural debt (shrinking is the metric)
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
