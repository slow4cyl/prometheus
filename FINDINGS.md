# Findings — a labeled snapshot

**Snapshot: July 2026, reference deployment.** Everything below is queried
straight from the live ledgers (`knowledge_claims`, `discovery_candidates`,
`world_groundings`) — machine-produced statuses, no hand-picking beyond
"top-N by the system's own scores." The point of this page is not the
findings themselves; it's the **labels**: every entry states what kind of
evidence backs it, and the failures are listed with the same prominence as
the survivors.

Scale at snapshot: **76,971 claims** extracted from **131,015 experiments**;
**127** claims promoted to the discovery shelf; **54** world-grounding
re-tests against external datasets.

---

## Reality's refusals — simulation said yes, real data said no

The system's most valuable output. Each of these survived internal
validation — real code, real sweeps, internally coherent — and then **failed
when re-tested against a real external dataset** with the loader code
mechanically verified. This is the toy-vs-world gap, itemized:

| The claim (as asked) | Real-world test | Verdict |
|---|---|---|
| Wavelet denoising preserves adversarial detection signatures in audio | ESC-50 (80 real environmental recordings, 4 perturbation types) | **FAILS** — all perturbation types showed high preservation after denoising; the simulation's discrimination vanished |
| Weight-noise collapse correlates with network depth vs width | MNIST + Fashion-MNIST (OpenML) | **FAILS** |
| Measurement-axis projection predicts which sensitivity analyses succeed in peer review | ICLR 2024 OpenReview corpus (7,210 papers) | **FAILS** — simulation: 89.2% vs 30.9% acceptance split; real data: 30.9% vs 33.8% (no effect) |
| Feature dimensions transfer across sensing modalities | UCI HAR (smartphone activity recognition) | **FAILS** |
| Complexity matching transfers to datasets with unknown true complexity | OpenML benchmark suite (18 classification datasets) | **FAILS** |
| kNN-graph label propagation gains generalize to sentence-transformer embeddings | AG News (HuggingFace) | **FAILS** — embeddings are already semantically structured; graph propagation added noise |

## Verified world holds — claims that survived contact

| The claim (as asked) | Real-world test | Verdict |
|---|---|---|
| Overparameterization ratio scales with model size | 46 published LLMs (official papers/model cards) | **HOLDS** |
| Gradient-norm magnitude predicts training instability better than sparsity | Pythia training metrics (HuggingFace) | **HOLDS** |
| Reasoning models handle well-known mathematical facts correctly (scope-mapped) | MMLU (cais/mmlu) | **HOLDS** |
| Over-conformity paradox reversal boundary | Old Faithful eruption times (N=272, bimodal) | **HOLDS** |
| Rank–learning-rate coupling applies to data-parallel training | Fashion-MNIST (OpenML #40996) | **HOLDS** |
| P_m correction factor is geometry-independent | SNAP (Stanford Large Network Dataset Collection) | **HOLDS** |

Verified-basis agreement at snapshot: **15/21 ≈ 71%**. The other ~29% is the
reason the lane exists.

## Discovery shelf — top of the internal ranking (honest caveat attached)

Top 8 by `discovery_score` (the system's own composite of attack survival,
independence, decisiveness). **Caveat, per the system's own measurement:**
the shelf is ~98% simulation-internal — these are mechanisms established in
worker-designed experiments, currently being hardened and world-grounded,
not peer-reviewed results. `survivals` counts adversarial attacks survived.

| Score | Survivals | Claim (question form) |
|---|---|---|
| 81.1 | 1 | Under what noise level does domain expertise flip from asset to liability? |
| 80.6 | 2 | Does weight-initialization scheme affect the steepness–width relationship? |
| 76.7 | 2 | Does the corrected CV formula transfer to other scale-free network models? |
| 76.6 | 1 | Do layer-interaction effects explain why predictor-guided hybrid defense underperforms? |
| 75.8 | 1 | Does noise heterogeneity predict training instability better than aggregate B? |
| 75.3 | 2 | Does weight-noise collapse extend to deep networks (not just LR/GB)? |
| 74.5 | 1 | Does supervision-regime asymmetry hold for Isolation Forests and autoencoders? |
| 74.2 | 1 | Does mechanism-specific transfer prediction outperform universal features? |

## The knowledge topology

The claim graph (nodes = claims, edges = support/contradiction/lineage) is
rendered as self-contained pages, regenerated hourly on the reference
deployment. Static snapshots of both are in this repo and served via GitHub
Pages — see the README's *Findings & live pages* section. The degree
distribution's straight-line descent on the log-log view is a self-recomputing
Clauset power-law fit, not a drawn caption.

---

*Generated from the reference deployment's databases at snapshot time. The
live numbers move; the labels' honesty is the invariant.*
