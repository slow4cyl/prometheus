# Batch Creator Returns 0 With Many Free Workers

**Date:** Cycle #244.5 (June 3, 2026)
**Symptom:** `batch_create_tasks.py --count 15 --dry-run` reports "Uncovered by running tasks: 0, Selected: 0 tasks" while 42 workers are free and 50 queue items exist.
**Root cause:** The Jaccard overlap thresholds (hypothesis >0.45, running task overlap >0.45) match too broadly. When 8+ running tasks cover common injection/attack topics, the batch creator considers ALL queue items "covered" because they share keywords like "injection", "detection", "ensemble" with running tasks.

## The Gap in Existing Documentation

The `manual-fallback-for-coverage-false-positives.md` reference covers the case where batch selects items that overlap running tasks. This reference covers the opposite: batch selects NOTHING because everything appears covered. Both are manifestations of the same overly-aggressive coverage check, but the fix differs:

- **False positives (selects overlapping items):** Filter selected items with 3-word phrase overlap
- **False negatives (selects nothing):** Manually create tasks for items that explore genuinely different angles on the same broad topic

## Decision Pattern: "Same Topic, Different Angle"

When batch returns 0, evaluate each queue item against running tasks using this hierarchy:

```
Running task: "Do multi-turn defense mechanisms generalize across different LLM architectures?"
Queue item:   "Can fine-tuning improve multi-turn resistance for smaller models like Qwen2.5-0.5B?"

Word overlap: HIGH (multi-turn, defense/resistance, mechanisms)
Topic overlap: LOW (generalization across architectures vs. fine-tuning for specific model size)
→ SAFE TO CREATE: Different methodology, different target
```

```
Running task: "What is the optimal PCA dimensionality cutoff for TF-IDF injection detection?"
Queue item:   "Does neural PCA 1D advantage hold for non-English embeddings?"

Word overlap: MEDIUM (PCA, injection detection)
Topic overlap: LOW (TF-IDF PCA vs. neural PCA cross-lingual)
→ SAFE TO CREATE: Different PCA variant, different language scope
```

```
Running task: "What is the optimal PCA dimensionality cutoff for TF-IDF injection detection?"
Queue item:   "What is the optimal character n-gram range for typo robustness?"

Word overlap: LOW (different methods entirely)
Topic overlap: NONE
→ SAFE TO CREATE: Completely different investigation
```

## When NOT to Create (Despite Free Workers)

Even with free workers, do NOT create tasks when:
1. Queue item is a **direct follow-up** of a running task (e.g., "does X generalize?" when "X" is currently running)
2. Queue item investigates the **exact same method** on the **exact same dataset** as a running task
3. Queue item was already created in a previous cycle and is currently running (check `hermes kanban list --status running`)

## Worked Example (Cycle #244.5)

**Board state:** 8 running tasks covering rephrasing robustness, adversarial training, ECE thresholding, SimCLR+TF-IDF, multi-turn defense, 22-word vocabulary, PCA dimensionality. 42 free workers.

**Batch creator output:** 0 tasks (all 33 score≥60 items deemed "covered").

**Manual creation — 6 tasks for genuinely different angles:**

| Running Task | Created Task | Why Different |
|---|---|---|
| exp_3257: multi-turn defense generalize across LLMs | exp_3265: fine-tuning multi-turn for small models | Generalization vs. specific model improvement |
| exp_3260: 22-word vocab non-English | exp_3266: 22-word vocab + Unicode normalization | Non-English vs. combined detector pipeline |
| exp_3261: optimal PCA dimensionality | exp_3267: 1D subspace cross-domain transfer | Dimensionality vs. domain transfer |
| (none) | exp_3262: homoglyph attacks char-level | Completely new attack vector |
| (none) | exp_3263: ensemble detectors cross-lingual | Completely new detector combination |
| (none) | exp_3264: leet speak neural embeddings | New embedding comparison |

**Result:** All 6 tasks dispatched successfully. 3 spawned immediately, 3 on next dispatch tick.

## Integration with Director Flowchart

This pattern applies at **step 6 (CREATE EXPERIMENT TASKS)** when the batch creator returns 0:

```
6. CREATE EXPERIMENT TASKS
   PREFERRED: batch_create_tasks.py --dry-run first
   ...
   If batch returns 0 uncovered at ANY --min-score:
     → DO NOT give up or leave workers idle
     → OPTION A: Manually evaluate queue items using "same topic, different angle" hierarchy
     → OPTION B: Probe items with pre_check_dedup.py (faster, more reliable)
     → Create tasks for items that pass
     → Assign to free workers
```

## Option B: Probe Individual Items with pre_check_dedup.py (June 5 2026)

When batch returns 0 but many workers are free, probe queue items individually:

```bash
for hyp in "queue item 1" "queue item 2" ...; do
  python3 ~/.hermes/scripts/pre_check_dedup.py "$hyp" 2>&1
  # EXIT=0 = pass (item is genuinely new)
  # EXIT=1 = blocked (item overlaps completed experiments)
done
```

**Why this works when batch fails:** The batch creator's "uncovered" check uses running+blocked+ready task titles at threshold 0.75. Individual `pre_check_dedup.py` checks against completed experiments in prometheus.db only. An item can be "covered" by the batch's running-task overlap but still pass individual dedup if no completed experiment answers the same question.

**Observed (June 5 2026):** Batch returned 0 uncovered with 44 free workers. Probing 26 items found 7 that passed. Created exp_101768–exp_101774.

**When to use Option B vs A:**
- Option B (probe): Batch returns 0 AND ≥20 free workers. Faster (~75s for 15 probes). Reliable — binary pass/fail.
- Option A (manual eval): Batch returns 0 AND <20 free workers. Requires judgment about "different angle." Slower but catches items probe misses (genuinely different angles on same completed topic).

**Decision rule:** If probing finds ≥3 items in 15 probes → use Option B results. If 0-2 items pass in 15 probes → queue is genuinely exhausted, stop.

## Option C: Deadlock — All Probing Fails (Cycle 246, June 5 2026)

When BOTH batch creator returns 0 AND individual pre_check_dedup.py probing
blocks ALL items (exit code 1 for every queue item), the system is in a
structural deadlock. This happens when the completed experiment corpus (7,784+)
is large enough that the phrase overlap threshold (0.30) catches even genuinely
novel follow-up questions.

**Detection:**
```
# Batch returns 0:
batch_create_tasks.py --count 15 → "Uncovered by all dedup layers: 0"

# Individual probing also fails:
for hyp in "item1" "item2" ...; do
  pre_check_dedup.py "$hyp"  # ALL return exit code 1
done

# But workers are idle:
hermes kanban list --status running  # Only 4 running, 46 free
```

**Key distinction from "genuine exhaustion":** Queue items are NOT stale
(queue_curator.py finds 0 stale). They're novel questions that share
vocabulary with completed experiments. The dedup system is working as
designed but has become a bottleneck at scale.

**Resolution options (see also director-loop skill `references/dedup-induced-deadlock-cycle246.md`):**
1. BUILD mode — implement findings from BUILD-tagged experiments
2. Relax PHRASE_OVERLAP_THRESHOLD from 0.30 to 0.45 (risks duplicates)
3. Queue refresh — manually add genuinely novel items with zero phrase overlap
4. Wait for synthesis to generate new items (but synthesis may be blocked)
