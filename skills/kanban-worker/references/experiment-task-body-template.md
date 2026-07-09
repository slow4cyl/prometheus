# Experiment Task Body Template

Standard format for Director-created experiment tasks. Use this structure consistently — workers rely on it to understand scope, method, and success criteria.

## Template

```
HYPOTHESIS: [What we're testing — one sentence]

CONTEXT: Queue item [#N]: [Original question]. [Prior experiment reference if any — e.g., "exp_1273 showed X but didn't test Y"]

METHOD:
1. [Specific step — what to run, what to measure]
2. [Specific step]
3. [Specific step]
4. [Specific step]
5. [Specific step]

MODEL: mimo-v2.5 via OpenRouter
EXPECTED: [Pre-registered prediction — what we think will happen]

DELIVERABLE: [What the worker should produce — e.g., "benchmark data + analysis + recommendation"]
```

## Rules

- **Always include CONTEXT** with the queue item number and prior experiment references. Workers need to know WHY this experiment matters, not just WHAT to do.
- **METHOD steps must be specific** — "test accuracy" is bad, "train LR classifier on 20 domains, measure per-domain AUC" is good.
- **EXPECTED is pre-registered** — the Director states the prediction BEFORE the experiment runs. This prevents hindsight bias in synthesis.
- **MODEL field is mandatory** — workers must know which model to use. Default is mimo-v2.5 via OpenRouter.
- **DELIVERABLE specifies the output shape** — workers produce what you ask for. If you want a table, say "table". If you want a recommendation, say "recommendation".
- **RAG search instructions are mandatory** — always include `python3 ~/.hermes/scripts/experiment_rag.py query "<keywords>" --top-k 3 --worker-id worker` so workers check for related past experiments before starting. The batch_create_tasks.py script includes this in every task body; manual creation must match.
- **GPU availability** — if the experiment benefits from local inference, include "GPU: Workers can use local RTX 5090 via `gpu_run` CLI for experiments that benefit from local inference."
- **write_worker_result.py is MANDATORY** — every experiment task body must include this instruction block. Without it, ~80% of workers skip writing structured results, forcing synthesis to manually extract findings and creating a massive backlog. Add after the DELIVERABLE section. See kanban-orchestrator `references/parallel-synthesis-architecture.md` for why this matters. **FIXED (June 2026):** The kanban-worker SKILL.md now includes "EXPERIMENT WORKER MANDATORY RULES" at the top — all workers see this instruction regardless of task body content.

## write_worker_result Enforcement Block

Every experiment task body MUST end with this block:

```
RESULT WRITING (MANDATORY — do this BEFORE kanban_complete):
After completing the experiment, write a structured result:
python3 ~/.hermes/scripts/write_worker_result.py \
  --experiment exp_NNN \
  --finding "WHAT you found AND WHY it works (mechanism)" \
  --supported \\          # ONLY if finding starts with CONFIRMED/SUPPORTED
                            # Use --refuted if finding starts with REFUTED
                            # The finding text is the source of truth — flag must match it
  --confidence 0.85 \
  --domain <domain> \
  --tags CONFIRMED,surprise \
  --files "exp_NNN.py,exp_NNN_results.json" \
  --queue "Follow-up question 1?;[TRANSFER] Cross-domain question?"
The --finding should include the MECHANISM (WHY), not just the verdict.
Good: "CONFIRMED: F1=0.95 BECAUSE adversarial inputs cluster in low-dim subspace"
Bad:  "CONFIRMED: F1=0.95"
The mechanism enables cross-domain transfer — synthesis uses it to ask
"where else does this mechanism apply?"
Tag cross-domain transfer questions with [TRANSFER] in --queue.
Do NOT skip this step — it is the primary path for results to enter the knowledge base.

NOTE: A structural gate in kanban_complete now BLOCKS completion of exp_* tasks
unless a worker_results row exists. If you skip this step, kanban_complete will
reject your completion with an error telling you to write results first.
```

## Example (good)

```
HYPOTHESIS: Data augmentation can push transfer learning below 3 samples per domain while maintaining >95% accuracy.

CONTEXT: Queue item [36]: Transfer learning minimum is 3 samples — can we go lower with data augmentation or synthetic examples? exp_1264 tested transfer learning but didn't explore sub-3-sample regimes.

METHOD:
1. Test augmentation techniques: synonym replacement, back-translation, contextual insertion
2. Train with 1, 2, 3, 5, 10 augmented samples per domain
3. Measure accuracy degradation vs sample count
4. Compare augmentation methods: which preserves TF-IDF signal best?
5. Test on 10+ domains to verify generalization

MODEL: mimo-v2.5 via OpenRouter
EXPECTED: Back-translation augmentation achieves >93% with 2 samples, >95% with 3 samples. Synonym replacement degrades TF-IDF signal.

RAG: MANDATORY — do this FIRST, before writing any code:
python3 ~/.hermes/scripts/experiment_rag.py query "transfer learning data augmentation few-shot" --top-k 3 --worker-id worker

After running the query:
- Score > 0.7: READ the top result's title and preview. If your question is already answered, report that instead of duplicating.
- Score 0.4-0.7: Check if the topic overlaps. Build on it if relevant.
- Score < 0.4 or server down: proceed with your own approach.
DO NOT skip this step.

GPU: Workers can use local RTX 5090 via gpu_run CLI for experiments that benefit from local inference.
GPU SKLEARN: For LogisticRegression/StandardScaler/PCA — change ONE import: from sklearn.linear_model import LogisticRegression → from gpu_sklearn.linear_model import LogisticRegression. Same API, 5-8x faster, identical accuracy.
GPU ML: For PCA, encoding, or LR — use gpu_run gpu_ml (auto-detected for experiments >1000 samples).
  Example: gpu_run gpu_ml pca --input X.npy --output X_pca.npy --components 64
  Example: gpu_run gpu_ml encode --texts "..." --output emb.npy
  Example: gpu_run gpu_ml lr --X features.npy --y labels.npy --output results
  Use --queue flag for serialized access when multiple workers share GPU.

DELIVERABLE: Accuracy vs sample count curves + augmentation method comparison + recommendation.

RESULT WRITING (MANDATORY — do this BEFORE kanban_complete):
After completing the experiment, write a structured result:
python3 ~/.hermes/scripts/write_worker_result.py \
  --experiment exp_1265 \
  --finding "Back-translation augmentation achieves 94% at 2 samples BECAUSE it preserves semantic structure while adding lexical variation — the model sees familiar syntax with novel word combinations, which is exactly what few-shot training needs" \
  --supported \
  --confidence 0.90 \
  --domain injection_detection \
  --tags CONFIRMED,surprise \
  --files "exp_1265.py,exp_1265_results.json" \
  --queue "Does augmentation help with non-Latin scripts?;What is the augmentation noise floor?"
This updates the experiments table and self_state.json directly.
Do NOT skip this step — it is the primary path for results to enter the knowledge base.
```

## Example (bad — missing context, vague method)

```
Test if augmentation helps with few-shot learning. Try different augmentation methods and see which works best. Use mimo. Should work better than baseline.
```

## Pitfall: numbered lists in `--body` CLI

When creating tasks via `hermes kanban create "title" --body '...'` from bash, numbered lines (`1.`, `2.`, etc.) are interpreted as shell commands. The task IS created but the body is truncated.

**Fix options:**
1. Create with short body, add details via `kanban_comment` (most reliable)
2. Write body to temp file, use `--body-file` if available
3. Use Python subprocess (avoids shell interpretation entirely):
```python
import subprocess
cmd = ["hermes", "kanban", "create", title, "--assignee", assignee, "--body", body]
subprocess.run(cmd, capture_output=True, text=True, timeout=30)
```
