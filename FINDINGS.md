# Findings — a labeled snapshot

**Snapshot: July 2026, reference deployment.** Everything below is queried
straight from the live ledgers (`knowledge_claims`, `discovery_candidates`,
`world_groundings`) — machine-produced statuses, no hand-picking beyond
"top-N by the system's own scores." The point of this page is not the
findings themselves; it's the **labels**: every entry states what kind of
evidence backs it, and the failures are listed with the same prominence as
the survivors.

Scale at snapshot (refreshed 2026-07-14): **82,486 claims** (non-MERGED, after
collapsing 1,317 duplicate hypothesis-fragments into their canonical claims)
extracted from **141,580 experiments**; **128** claims promoted to the
discovery shelf; **55** world-grounding re-tests against external datasets,
**29** of them with the loader code mechanically verified.

---

## Reality's refusals — simulation said yes, real data said no

The system's most valuable output. Each of these survived internal
validation — real code, real sweeps, internally coherent — and then **failed
when re-tested against a real external dataset** with the loader code
mechanically verified. This is the toy-vs-world gap, itemized:

| The claim (as asked) | Real-world test | Verdict |
|---|---|---|
| Wavelet denoising preserves adversarial detection signatures in audio | ESC-50 (80 real environmental recordings, 4 perturbation types) | **FAILS** — all perturbation types showed high preservation after denoising; the simulation's discrimination vanished |
| Weight-noise collapse correlates with network depth vs width | MNIST (OpenML #554) + Fashion-MNIST | **FAILS** |
| Measurement-axis projection predicts which sensitivity analyses succeed in peer review | ICLR 2024 OpenReview corpus (7,210 papers) | **FAILS** — simulation: 89.2% vs 30.9% acceptance split; real data: 30.9% vs 33.8% (no effect) |
| Feature dimensions transfer across sensing modalities | UCI HAR (smartphone activity recognition) | **FAILS** |
| Complexity matching transfers to datasets with unknown true complexity | OpenML benchmark suite (18 classification datasets) | **FAILS** |
| kNN-graph label propagation gains generalize to sentence-transformer embeddings | AG News (HuggingFace) | **FAILS** — embeddings are already semantically structured; graph propagation added noise |
| Batch-norm contamination becomes significant at higher batch variability | MNIST (torchvision) | **FAILS** — max eval-batch gap 0.0046, far below the claimed 0.023 |
| Complexity-coupling interaction generalizes across datasets | OpenML benchmark suite (28 datasets) | **FAILS** |
| Structural-vs-scale distinction explains PCA's sensitivity to correlated features | UCI (Wine, Breast Cancer, Digits) | **FAILS** |

Verified FAILS at snapshot: **9** (the loader code mechanically confirmed to
read real external data). Each is a mechanism that was internally coherent and
died on contact with a dataset.

## Verified world holds — claims that survived contact

| The claim (as asked) | Real-world test | Verdict |
|---|---|---|
| Overparameterization ratio scales with model size (α = 0.73) | 46 published LLMs (official papers/model cards) | **HOLDS** |
| Gradient-norm magnitude predicts training instability better than sparsity | Pythia training metrics (HuggingFace) | **HOLDS** |
| Reasoning models handle well-known mathematical facts correctly (scope-mapped) | MMLU (cais/mmlu) | **HOLDS** |
| Over-conformity paradox reversal boundary | Old Faithful eruption times (N=272, bimodal) | **HOLDS** |
| Rank–learning-rate coupling applies to data-parallel training | Fashion-MNIST (OpenML #40996) | **HOLDS** |
| P_m correction factor is geometry-independent | SNAP (Stanford Large Network Dataset Collection) | **HOLDS** |
| Robustness–accuracy tradeoff holds for certified defenses | ImageNet + CIFAR-10 (Cohen et al. 2019 published results) | **HOLDS** |
| Variational mode decomposition works with alternative variance estimators | GWOSC GW150914 H1 strain (real gravitational-wave data) | **HOLDS** |
| N_50 ensemble-disagreement bound stays at 2–5 for neural nets | MNIST (OpenML mnist_784) | **HOLDS** |

Verified-basis agreement at snapshot: **20/29 ≈ 69%**. The other ~31% is the
reason the lane exists — internally-validated mechanisms that a real dataset
refused. (The rate moved from 15/21 ≈ 71% as the mechanically-verified pool
grew from 21 to 29 outcomes; the sample is small and the point is the labels,
not the exact percentage.)

## Discovery shelf — top of the internal ranking (honest caveat attached)

The full shelf by `discovery_score` (the system's own composite of attack
survival, independence, decisiveness) after this month's gate cascade — the
claim-reconciliation and spurious-support gates moved every entry whose
headline contradicted its own mapped scope, or whose supports only agreed on
the flag, into visible off-shelf bins, leaving a smaller and more defensible
set. **Caveat, per the system's own measurement:** the shelf is ~98%
simulation-internal — mechanisms established in worker-designed experiments,
now being hardened and world-grounded, not peer-reviewed results. `survivals`
counts adversarial attacks survived; every entry here is ESTABLISHED.

| Score | Survivals | Claim (question form) |
|---|---|---|
| 93.7 | 5 | Does inversion strength correlate with feature dimensionality? |
| 74.8 | 1 | Does mechanism-specific transfer prediction outperform universal features? |
| 73.8 | 1 | Does the supervision-regime asymmetry hold for Isolation Forests and autoencoders? |
| 65.9 | 1 | Does gradient-norm magnitude predict training instability better than sparsity? |
| 61.3 | 1 | Does E_f interact with structural context to predict tolerance? |
| 59.9 | 1 | Under what conditions does the regime-dependence finding fail? |

Five of these six now carry a **verified world-grounding HOLDS** — #65238 on
20 Newsgroups, #60779 on OpenML meta-features, #58935 on Pythia training
metrics, #65514 on a HuggingFace protein-stability set, #54615 on UCI Bike
Sharing — while #66039 is still queued in the world-grounding lane by
construction. That the top of the internal ranking now largely survives
external contact is the point of running the lane at all.

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
