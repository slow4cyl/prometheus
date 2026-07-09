# Launch post — DRAFT

> Status: **draft, not posted anywhere.** Three variants below: an HN-style text
> post, an X thread, and a 2-sentence tl;dr. Numbers are the July 2026 snapshot
> from `FINDINGS.md`; if posting later, re-check them against the live dashboard
> first.

---

## Variant 1 — HN text post (~250 words)

**Suggested titles (pick one):**
- Show HN: An autonomous research system that measures how often it fools itself
- Show HN: My research agent predicts its own claim-transfer at 53%. Publishing that is the point

**Body:**

I built an autonomous research system. Before the architecture, here is what it measured about itself:

- Asked to predict which of its own claims would transfer to new domains, it scored 53%, barely above chance. It responded by hair-cutting transfer confidence across the board.
- A self-audit found ~98% of its discovery shelf had never touched real-world data. Those claims were validated only against simulation code the workers wrote themselves.
- So it now re-tests simulation-validated claims against real external datasets (OpenML, HuggingFace, UCI, the ICLR OpenReview corpus). Only 15 of 21 verified re-tests survive, about 71%. The six failures are catalogued as first-class results, "reality's refusals", not deleted.
- A calibration audit found it over-trusts MONOTONIC-shaped claims 67.6% of the time. It reweighted them.

The system runs continuously on one consumer gaming PC with an RTX 5090. It generates its own questions, runs experiments with preserved code (~131k experiments, ~77k claims at snapshot), and spends a large share of its compute attacking its own conclusions: adversarial replication, cross-domain disconfirmation gates, novelty checks against actual literature indexes, independence and circularity gates.

For context: this is my first project. No CS degree, about a month of work. It sits on top of the open-source hermes-agent substrate, which it requires.

My read of the field is that the generators have outrun the validators. Every agent produces "discoveries" now. The interesting problem is honesty, and this system's headline feature is that it distrusts itself and publishes the receipts.

Repo: https://github.com/slow4cyl/prometheus
Requires: https://github.com/NousResearch/hermes-agent

---

## Variant 2 — X thread (7 tweets)

**1/**
I built an autonomous research agent. Its headline feature: it measures how often it fools itself, and publishes the numbers.

Asked to predict which of its own claims transfer to new domains, it scored 53%. Barely above chance. It cut its own confidence in response.

**2/**
Then it audited its "discovery shelf": ~98% of claims had never touched real-world data. Validated only against simulation code it wrote itself.

I suspect that describes most autonomous-research agents right now. Mine just checked.

**3/**
So it re-tests simulation-validated claims against real external datasets (OpenML, HuggingFace, UCI, OpenReview).

15 of 21 verified re-tests survive, ~71%. The 6 failures are catalogued as "reality's refusals" with the same prominence as the survivors. Not buried.

**4/**
My favorite refusal: in simulation, one mechanism predicted sensitivity-analysis outcomes in peer review with an 89.2% vs 30.9% split. Re-tested on 7,210 real ICLR 2024 papers: 30.9% vs 33.8%. No effect.

Simulation said yes. Reality said no. Both are in the ledger.

**5/**
It also audits its own calibration by claim shape. Finding: MONOTONIC-type claims ("more X, more Y") were over-trusted 67.6% of the time, so it reweighted them.

Contradicted claims aren't deleted either. They get routed to an attack lane and fought over.

**6/**
Context: this is my first project. No CS degree. About a month, one consumer gaming PC with an RTX 5090. ~131k experiments, ~77k claims, ~90 cron jobs, a local vLLM worker fleet plus an API lane. Built on the open-source hermes-agent substrate.

**7/**
The field's generators have outrun its validators. Every agent ships "discoveries". The hard, interesting problem is honesty.

Repo (requires hermes-agent): https://github.com/slow4cyl/prometheus
Substrate: https://github.com/NousResearch/hermes-agent

---

## Variant 3 — tl;dr (2 sentences)

Prometheus is an autonomous research system (first project, about a month, one RTX 5090, built on the open-source hermes-agent substrate) whose headline feature is self-distrust: it measured itself at 53% at predicting which of its own claims transfer to new domains and cut its confidence accordingly, found ~98% of its discovery shelf had never touched real-world data, and saw only ~71% (15 of 21) of its simulation-validated claims survive re-testing against real external datasets. The failures are published with the same prominence as the survivors, because the field's generators have outrun its validators and the interesting problem now is honesty.
