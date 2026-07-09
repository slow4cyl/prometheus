# Semantic Verification of Queue Coverage (Cycle #245)

## The gap

The pre-creation topic-text cross-reference (Cycle #253) uses keyword overlap scoring to detect whether a queue item's topic is already covered by running experiments. This works for obvious keyword matches ("INT1" in both item and title) but misses cases where the *question* is the same but the *wording* differs significantly.

## Manual semantic verification pattern

After quantitative coverage scoring returns LOW/MED, perform a manual semantic check: read each running experiment's title and verify whether it is *directly investigating the same question* as the queue item. The queue item and running experiment may use different words but target the same hypothesis.

**When to apply:** For every queue item with coverage score MED (0.15-0.3) or when the scorer returns LOW but you suspect overlap. This catches false negatives from keyword scoring.

**Concrete example (Cycle #245):**

Queue item [19]: "2-feature INT1 achieves 100% at 2.3us — can this be deployed as the universal first-pass filter for ALL injection detection pipelines?"
Running exp_1553: "INT1 standalone vs cascade — theoretical framework for why simpler beats complex"

Keyword overlap: LOW (shared words: "INT1", "standalone" — score ~0.18)
Semantic match: HIGH — exp_1553 is testing whether INT1 is sufficient as a standalone detector, which is the same question as "can INT1 be deployed as universal first-pass filter"
Action: SKIP — item will resolve when exp_1553 completes

Queue item [24]: "TF-IDF+LR standalone preferred over streaming hybrid (exp_1543) — does this simplicity advantage hold at 100+ domains?"
Running exp_1550: "Streaming 6-feature detector adversarial robustness"

Keyword overlap: LOW (shared words: "streaming" — score ~0.12)
Semantic match: LOW — exp_1550 tests adversarial robustness, not scaling to 100+ domains. Different question entirely.
Action: CREATE TASK — item is genuinely uncovered

**Verification heuristic:** Ask "If this running experiment completes successfully, will it answer the queue item's specific question?" If yes → SKIP. If no → CREATE.

## Decision matrix

| Keyword score | Semantic match | Action |
|:---:|:---:|:---|
| HIGH (>0.3) | Any | SKIP (definitely covered) |
| MED (0.15-0.3) | HIGH | SKIP (covered despite low keywords) |
| MED (0.15-0.3) | LOW | CREATE (different question, low word overlap) |
| LOW (<0.15) | HIGH | SKIP (same question, different vocabulary) |
| LOW (<0.15) | LOW | CREATE (genuinely uncovered) |

## Real-world outcome (Cycle #245)

- 16 active queue items classified against 8 running experiments
- 12 items identified as covered (source experiment running OR semantic match with running experiment)
- 4 items identified as genuinely uncovered → 3 tasks created (exp_1557, exp_1558, exp_1559)
- 0 duplicate tasks created
- All active queue items covered after task creation

## Key insight

The semantic verification is especially important when running experiments use different vocabulary than queue items. Queue items come from synthesis findings (which use domain-specific language), while experiment titles come from the Director (which uses more technical language). The same question can appear very different in these two contexts.
