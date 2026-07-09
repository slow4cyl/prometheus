---
name: synthesis-task-body-template
description: Standard format for Director-created synthesis tasks. Includes cross-pollination reasoning for lateral exploration.
version: 3.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, synthesis, cross-pollination, curiosity]
---

# EXPERIMENT WORKER MANDATORY RULES

**write_worker_result.py is MANDATORY for ALL experiment tasks.**

After completing ANY experiment, you MUST call:
```
python3 ~/.hermes/scripts/write_worker_result.py \
  --experiment <exp_id from task title> \
  --finding "WHAT you found. WHY IT WORKS: <mechanism explanation>. [TRANSFER] <cross-domain question if applicable>" \
  --supported \
  --confidence <0.0-1.0> \
  --domain <domain> \
  --type <MECHANISTIC|EMPIRICAL|ANALOGICAL> \
  --mechanism-type <UNIVERSAL_LAW|EMPIRICAL_CORRELATION> \
  --tags CONFIRMED,DISCOVERY,etc \
  --files "file1.py,file2.json" \
  --queue "Follow-up 1?;[TRANSFER] Cross-domain question?" \
  --predicted-direction '{"your_prediction": 1}' \
  --observed-direction '{"your_observation": 1}' \
  --design-vector '{"instrument": "TF-IDF+LR", "distribution": "code_mixed", "metric": "F1"}'
```

**Experiment types (required):**
- `MECHANISTIC` — testing if a known mechanism is sufficient (synthetic data OK, high R² expected)
- `EMPIRICAL` — testing a prediction against real-world data (synthetic data invalid, high R² suspicious)
- `ANALOGICAL` — testing whether a mechanism from domain A operates in domain B (the [TRANSFER] experiments)

**Mechanism types (for ANALOGICAL only):**
- `UNIVERSAL_LAW` — first-principles math (thermodynamics, info theory, wave physics). Gets 1.3x scoring priority.
- `EMPIRICAL_CORRELATION` — fitted relationships (biology scaling, materials properties). Gets 1.0x priority.

**ANALOGICAL experiment approach:**
1. Identify the MECHANISM from the source experiment (not just the finding)
2. Determine if that mechanism's assumptions hold in the target domain
3. Design a test that would FAIL if the mechanism doesn't transfer
4. Run the test and report whether the mechanism generalizes

**Verdict types:**
- `CONFIRMED` / `SUPPORTED` — hypothesis tested and confirmed
- `REFUTED` — hypothesis tested and refuted
- `REFUTED_SETUP` — experimental setup couldn't test hypothesis (architectural limitation)
- `REFUTED_HYPOTHESIS` — hypothesis was tested and failed (explicit)
- Use `REFUTED_SETUP` when the apparatus couldn't test the claim (wrong architecture, missing data, etc.)
- Use `REFUTED_HYPOTHESIS` when the claim was tested and found false

This writes to the worker_results table. A cron job applies it to the experiments database.
Without this step, your findings are LOST — they never reach the dashboard, synthesis, or knowledge base.

The --predicted-direction, --observed-direction, and --design-vector flags are optional but help the epistemic routing system classify your experiment's refutation_type. Use JSON dicts with numerical values (+1/-1/0). See director-loop skill references/epistemic-routing-system.md for details.

**Do NOT skip this step.** The experiment is not complete until write_worker_result.py is called.

**Quality gate (June 2026):** write_worker_result.py validates your finding via quality_validator.py. Scoring:
- **HAS_MECHANISM** (+25 bonus): finding includes "WHY IT WORKS:" or "MECHANISM:" with explicit causal explanation
- **NO_MECHANISM** (tagged, no penalty but lower score): has metrics (F1, AUC) but no mechanism explanation
- **Score <40**: REJECTED (exit code 2). Empty, self-referential, or junk results cannot be written.

**Your finding MUST include a WHY IT WORKS section.** Format:
```
CONFIRMED/REFUTED: <one-line verdict>. F1=<score>, AUC=<score>.
WHY IT WORKS: <mechanism — WHY is this true, not just WHAT was found>. Because <causal chain>.
```

High accuracy without mechanism explanation is NOT citable (exp_72526493). The mechanism is what makes a finding reusable by future-you and other researchers.

## PITFALL: Free-Tier Models Drop Result Writing (June 2026)

Workers using weak/free-tier models (e.g. `minimax-m3-free`) lose instruction
adherence over long task bodies. The `write_worker_result.py` step is at the
END of task bodies (200+ lines) — smaller models complete the experiment but
skip the mandatory result call. 94% of done tasks had no results (3,209/3,405).

**Detection:** `SELECT CASE WHEN result IS NOT NULL AND result != '' THEN 'has' ELSE 'no' END, COUNT(*) FROM tasks WHERE status='done' GROUP BY 1;`
If "no" >> "has", this is the problem.

**Structural enforcement (implemented June 2026):**
`_enforce_worker_result_written()` in `tools/kanban_tools.py` now blocks
`kanban_complete` on `exp_*` tasks unless a `worker_results` row exists.
This makes result-writing impossible to skip — the gate fires regardless
of model capability. Query checks `worker_results.experiment_id` against
the experiment ID extracted from the task title.

**Additional fixes:** Worker profiles upgraded from `minimax-m3-free` to
`xiaomi/mimo-v2.5`. Recovery sweep script at `scripts/recovery_sweep.py`
retroactively writes results for tasks that completed without them.

**Prevention:** The structural gate is the primary enforcement. Task body
instructions are a backup. Never rely on behavioral instructions alone
for critical pipeline steps — use structural gates.

See references/dashboard-experiment-count-lag.md Variant 2 for full diagnosis.

---

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
1. exp_NNN: <one-line result summary>
   WHY IT WORKS: <mechanism — WHY is this true, not just WHAT was found>
2. exp_MMM: <one-line result summary>
   WHY IT WORKS: <mechanism>
3. exp_OOO: <one-line result summary>
   WHY IT WORKS: <mechanism>

ANALYSIS:
- Cross-experiment patterns: what themes connect these results?
- Queue resolutions: which queue items did these experiments answer?
- Counter reconciliation: if counters drift, include --counters

CROSS-POLLINATION (do this AFTER analysis):
For each key finding, answer these questions for yourself:
1. What mechanism makes this finding true?
2. Where else does that mechanism show up — even outside security/injection?
3. What question do YOU want to answer next based on this?

Tag cross-domain transfer questions with [TRANSFER] in the curiosities.
These are questions that apply findings to entirely new domains.

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \\
  --task-id $HERMES_KANBAN_TASK \\
  --experiments "exp_NNN,exp_MMM,exp_OOO" \\
  --resolutions '[{"item": N, "resolved_by": "exp_XXX", "note": "what was found"}]' \\
  --curiosities "Follow-up 1?;[TRANSFER] Cross-domain question?;Follow-up 3?" \\
  --patterns "Cross-experiment pattern description"
```

## Real Example

```
SYNTHESIS CYCLE: Parallel synthesis via synthesis_outputs table.

DO NOT write to self_state.json directly — use write_synthesis_output.py instead.

Experiments to synthesize:
1. exp_3248: Raw ECE thresholding beats meta-classifiers — CONFIRMED
   WHY IT WORKS: Temperature scaling reveals calibration differences between
   genuine and adversarial outputs. Adversarial inputs produce less calibrated
   confidence distributions because the model hasn't learned them as a class.
2. exp_3259: Neural PCA 1D advantage CONFIRMED across 6 languages
   WHY IT WORKS: Adversarial text clusters in a low-dimensional subspace
   that's language-agnostic. The signal is structural (token distribution
   shift) not linguistic (specific words).
3. exp_3234: SVD component count inversely correlates with injection subtlety
   WHY IT WORKS: Subtle injections use fewer anomalous tokens, so the
   singular value decomposition captures less variance in the adversarial
   direction.

ANALYSIS:
- Pattern: Simple statistical methods (PCA, ECE, SVD) consistently beat
  complex ones. The model's internal representation has clear statistical
  signatures that don't require learned classifiers to detect.
- Queue resolutions: items about calibration thresholds answered by exp_3248

CROSS-POLLINATION:
1. ECE thresholding works because adversarial inputs shift calibration.
   Where else do inputs shift model calibration? Hallucination (model is
   uncertain but confident), bias (model is confident about stereotypes),
   out-of-distribution detection.
   → [TRANSFER] Does ECE thresholding detect hallucination in open-domain QA?
2. PCA finds structural signal independent of language. Where else is
   adversarial signal structural rather than linguistic? Prompt injection
   in code, adversarial examples in images (same principle — perturbation
   in a subspace).
   → [TRANSFER] Does PCA subspace detection work for adversarial code injection?
3. Simplicity beats complexity across all three experiments. Is this a
   general principle? Simple statistical baselines vs learned classifiers
   in other ML safety domains.
   → [TRANSFER] Do simple statistical methods beat learned detectors for
   jailbreak detection too?

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \
  --task-id t_abc123 \
  --experiments "exp_3248,exp_3259,exp_3234" \
  --resolutions '[{"item": 22, "resolved_by": "exp_3248", "note": "ECE thresholding confirmed"}]' \
  --curiosities "Does ECE thresholding detect hallucination in open-domain QA?;[TRANSFER] Does PCA subspace detection work for adversarial code injection?;[TRANSFER] Do simple statistical methods beat learned detectors for jailbreak detection?" \
  --patterns "Simple statistical methods consistently beat complex learned classifiers. The model's internal representation has clear statistical signatures for adversarial content."
```

## Key Patterns

1. **WHY IT WORKS is mandatory** — Don't just report what was found. Explain the mechanism. This is what enables cross-pollination. Without mechanism, transfer questions are guesswork.

2. **Cross-pollination asks THREE questions**:
   - What mechanism makes this true?
   - Where else does that mechanism apply?
   - What do YOU want to answer next?
   The third question is the most important — it's the system's own curiosity.

3. **[TRANSFER] tags identify cross-domain questions** — The curiosity scorer gives these a bonus. They're inherently more novel than same-thread follow-ups.

4. **Write to table, not self_state.json** — Multiple synthesis workers can write concurrently. The merger script is the single writer.

5. **Queue resolutions go in --resolutions flag** — JSON array of {item, resolved_by, note}.

6. **Cross-experiment analysis is the real value** — Don't just list results. Identify themes, connections, and transferable mechanisms across experiments.

7. **new_curiosities field format** — The `--curiosities` flag takes a semicolon-separated string, NOT a JSON array. Example: `"Follow-up 1?;[TRANSFER] Cross-domain question?;Follow-up 3?"`. When reading from synthesis_outputs table, split on semicolons: `curiosities = [c.strip() for c in curiosities_str.split(';') if c.strip()]`. Do NOT use `json.loads()` — it will fail silently and appear as 0 curiosities.

## synthesis_outputs Table Schema

The table column is `experiments_covered` (NOT `experiments`). Data is stored as JSON arrays:
```json
["exp_3052", "exp_3102", "exp_3154"]
```

**Correct parsing pattern:**
```python
import json
synthesized = set()
for row in conn.execute("SELECT experiments_covered FROM synthesis_outputs").fetchall():
    if row[0]:
        exp_list = json.loads(row[0])
        if isinstance(exp_list, list):
            for exp_id in exp_list:
                if isinstance(exp_id, str) and exp_id.startswith('exp_'):
                    synthesized.add(exp_id)
```

**Common mistake**: Using `SELECT experiments` (column doesn't exist) or splitting on commas (data is JSON, not CSV).

## Portable Installation

The kanban worker scripts (`write_worker_result.py`,
`batch_create_tasks.py`, etc.) are included in the portable
Prometheus package. When installing on a fresh machine, they
are installed to `~/.hermes/scripts/` automatically.

See `devops/portable-prometheus` for the complete deployment system.
