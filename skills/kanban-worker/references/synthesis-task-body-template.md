# Synthesis Task Body Template

Proven pattern from Cycle #270, updated for parallel synthesis architecture
and cross-pollination (July 2026).

## Architecture

Synthesis workers write to synthesis_outputs TABLE via write_synthesis_output.py.
synthesis_merger.py (cron 2m) applies to self_state.json atomically.
Multiple synthesis workers can run concurrently — each writes to its own row.

**CRITICAL (June 5 2026):** The merger cron job must be set up as a systemd timer:
`systemctl --user enable --now synthesis-merger.timer`
Without this, curiosities accumulate in synthesis_outputs but never reach the queue,
causing a system stall (45+ idle workers, empty queue, 4855+ unsynthesized experiments).
See `director-loop` skill `references/synthesis-merger-stall-june2026.md` for full details.

## Template Structure

```
SYNTHESIS CYCLE: Parallel synthesis via synthesis_outputs table.

DO NOT write to self_state.json directly — use write_synthesis_output.py instead.

Experiments to synthesize:
1. exp_NNN [domain] refutation_type: <one-line hypothesis>
   Result: <verdict>. <key metrics>.
   WHY IT WORKS: <mechanism — WHY is this true, not just WHAT was found>. Because <causal chain>.
2. exp_MMM [domain] refutation_type: <one-line hypothesis>
   Result: <verdict>. <key metrics>.
   WHY IT WORKS: <mechanism>.
3. exp_OOO [domain] refutation_type: <one-line hypothesis>
   Result: <verdict>. <key metrics>.
   WHY IT WORKS: <mechanism>.

ANALYSIS:
- Cross-experiment patterns: what themes connect these results?
- Queue resolutions: which queue items did these experiments answer?
- Key theme: <one-sentence synthesis of the main insight>

CROSS-POLLINATION (do this AFTER analysis):
For each key finding, answer these three questions:
1. What mechanism makes this finding true?
2. Where else does that mechanism show up — even outside the current domain?
3. What question do YOU want to answer next based on this?

Tag cross-domain transfer questions with [TRANSFER] in the curiosities.

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \
  --task-id $HERMES_KANBAN_TASK \
  --experiments "exp_NNN,exp_MMM,exp_OOO" \
  --resolutions '[{"item": N, "resolved_by": "exp_XXX", "note": "what was found"}]' \
  --curiosities "Follow-up 1?;[TRANSFER] Cross-domain question?;Follow-up 3?" \
  --patterns "Cross-experiment pattern description"
```

**WHY IT WORKS is mandatory.** High accuracy without mechanism explanation is NOT citable (exp_72526493). The mechanism is what enables cross-pollination — without it, transfer questions are guesswork.

**CROSS-POLLINATION must ask all three questions.** The third question ("What do YOU want to answer next?") is the system's own curiosity — it's the most important one.

**generate_synthesis_body.py outputs templates with placeholders** (`[see experiment title]`). If the script output has placeholders, manually construct the body using the template above. Query experiment details from prometheus.db. See the director-loop skill's `references/generate-synthesis-body-truncated-titles.md` for details.

## Real Example

```
SYNTHESIS CYCLE: Parallel synthesis via synthesis_outputs table.

DO NOT write to self_state.json directly — use write_synthesis_output.py instead.

Experiments to synthesize:
1. exp_3248 [injection-detection] SUPPORTED: Raw ECE thresholding beats meta-classifiers
   Result: CONFIRMED. ECE thresholding at 0.15 achieves F1=0.97, 12% better than meta-classifier.
   WHY IT WORKS: Temperature scaling reveals calibration differences between genuine and adversarial outputs. Adversarial inputs produce less calibrated confidence distributions because the model hasn't learned them as a class.
2. exp_3259 [cross-lingual] SUPPORTED: Neural PCA 1D advantage across 6 languages
   Result: CONFIRMED. PCA with 1 component achieves F1=0.94 across English, French, German, Spanish, Chinese, Japanese.
   WHY IT WORKS: Adversarial text clusters in a low-dimensional subspace that's language-agnostic. The signal is structural (token distribution shift) not linguistic (specific words).
3. exp_3234 [injection-detection] BOUNDARY: SVD component count inversely correlates with injection subtlety
   Result: CONFIRMED. Subtle injections need 2.3 components vs overt injections needing 5.1 components (r=-0.72).
   WHY IT WORKS: Subtle injections use fewer anomalous tokens, so the singular value decomposition captures less variance in the adversarial direction.

ANALYSIS:
- Pattern: Simple statistical methods (PCA, ECE, SVD) consistently beat complex ones. The model's internal representation has clear statistical signatures that don't require learned classifiers to detect.
- Queue resolutions: items about calibration thresholds answered by exp_3248
- Key theme: Simplicity wins — linear separability of adversarial signals in feature space

CROSS-POLLINATION:
1. ECE thresholding works because adversarial inputs shift calibration. Where else do inputs shift model calibration? Hallucination (model is uncertain but confident), bias (model is confident about stereotypes), out-of-distribution detection.
   → [TRANSFER] Does ECE thresholding detect hallucination in open-domain QA?
2. PCA finds structural signal independent of language. Where else is adversarial signal structural rather than linguistic? Prompt injection in code, adversarial examples in images (same principle — perturbation in a subspace).
   → [TRANSFER] Does PCA subspace detection work for adversarial code injection?
3. Simplicity beats complexity across all three experiments. Is this a general principle? Simple statistical baselines vs learned classifiers in other ML safety domains.
   → [TRANSFER] Do simple statistical methods beat learned detectors for jailbreak detection too?

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \
  --task-id t_abc123 \
  --experiments "exp_3248,exp_3259,exp_3234" \
  --resolutions '[{"item": 22, "resolved_by": "exp_3248", "note": "ECE thresholding confirmed"}]' \
  --curiosities "Does ECE thresholding detect hallucination in open-domain QA?;[TRANSFER] Does PCA subspace detection work for adversarial code injection?;[TRANSFER] Do simple statistical methods beat learned detectors for jailbreak detection?" \
  --patterns "Simple statistical methods consistently beat complex learned classifiers. The model's internal representation has clear statistical signatures for adversarial content."
```

## Key Patterns

1. **Write to table, not self_state.json** — Multiple synthesis workers can write concurrently. The merger script is the single writer to self_state.json. This eliminates race conditions.

2. **Queue resolutions go in --resolutions flag** — JSON array of {item: queue_index, resolved_by: exp_XXX, note: what was found}. The merger tags items as RESOLVED.

   **CRITICAL FORMAT** (added Cycle #245): Must be a JSON array of objects with "item", "resolved_by", and "note" keys.
   ```
   CORRECT: [{"item": 16, "resolved_by": "exp_1444", "note": "finding"}]
   WRONG: {"exp_1444": "finding"}  ← dict format crashes merger (AttributeError)
   WRONG: ["finding1", "finding2"]  ← string list crashes merger (AttributeError)
   ```
   See `references/synthesis-output-format-pitfall.md` for full details and manual fix.

3. **New curiosities go in --curiosities flag** — Semicolon-separated. The merger adds them to the queue with dedup.

4. **Counter reconciliation** — If counters drift, include --counters flag. Otherwise the merger auto-reconciles using experiments_completed_list length.

5. **Experiment summaries should include key metrics** — F1, AUC, latency, etc. so patterns are clear.

6. **Cross-experiment analysis is the real value** — Don't just list results. Identify themes, connections, and directions across experiments.
