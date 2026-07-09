#!/usr/bin/env python3
"""
Batch Task Creator — creates multiple Kanban tasks in one pass.

Reads scored queue items, filters by score and diversity, creates tasks.
The Director runs one command instead of N individual kanban_create calls.

Dedup layers (6 total):
  0.5. RAG semantic check — queries experiment_rag.py for close matches (score >0.90)
       OPTIMIZATION: Skip for items with RAG score < 0.5 (clearly novel).
  0.7. pre_check_dedup.py — aligns with safe_kanban_create's phrase_overlap dedup
       OPTIMIZATION: Skip for ANALOGICAL items and items with low RAG score.
  1. Checks queue items against completed experiments (hypothesis >0.65, result >0.65)
  2. Checks against running task titles (dynamic threshold: 0.60-0.75 based on free workers)
  3. Intra-batch: checks each candidate against already-selected items (overlap >0.65)
  4. Phrase-level: 30%+ of key phrases matching a single hypothesis

PERFORMANCE: Early exit at count × 3 uncovered candidates (~90 for default 30).
This reduces RAG calls from 263 to ~100, cutting total time from 240s to ~160s.

Data source: prometheus.db (primary), self_state.json (fallback)
RAG source: experiment_rag.py server (port 9150)

Usage:
    python3 batch_create_tasks.py                     # Create up to 30 tasks
    python3 batch_create_tasks.py --count 10          # Create up to 10 tasks
    python3 batch_create_tasks.py --min-score 70      # Only items scoring >= 70
    python3 batch_create_tasks.py --dry-run            # Show what would be created
    python3 batch_create_tasks.py --json               # JSON output
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import random
import hashlib
from pathlib import Path
from collections import defaultdict

# Import priority function from task_refiller
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from task_refiller import get_task_priority

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

# Add scripts dir to path for curiosity_scorer and gpu_route import

"""CLI tool: Batch Create Tasks.

Usage: python3 batch_create_tasks.py [options]
"""

SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPTS_DIR))

# Import GPU route detection
try:
    from gpu_route import detect_ops, generate_gpu_instructions
    GPU_ROUTE_AVAILABLE = True
except ImportError:
    GPU_ROUTE_AVAILABLE = False

# Import routing log for topology visualization
try:
    from domain_classifier import log_routing_decision
    ROUTING_LOG_AVAILABLE = True
except ImportError:
    ROUTING_LOG_AVAILABLE = False

# Import transfer tracking
try:
    from transfer_tracking import update_task_created, parse_transfer_target, get_transfer_backlog, compute_adaptive_transfer_cap
    TRANSFER_TRACKING_AVAILABLE = True
except ImportError:
    TRANSFER_TRACKING_AVAILABLE = False

SELF_STATE_PATH = os.path.expanduser("~/.hermes/self_state.json")
# Fleet size + lane split come from worker_config.py (the single dial).
# Override with PROMETHEUS_WORKER_COUNT env var. See worker_config.py.
try:
    from worker_config import WORKER_COUNT as MAX_WORKERS, worker_list, cycle_target
    WORKERS = worker_list()
except Exception:
    # Defensive fallback if worker_config is unavailable for any reason.
    MAX_WORKERS = int(os.environ.get("PROMETHEUS_WORKER_COUNT", "50"))
    WORKERS = ["default"]
    def cycle_target(count=None):
        return round((count or MAX_WORKERS) * 1.2)
SYNTHESIS_WORKER = "default"
DEFAULT_COUNT = max(cycle_target(), 50)  # At least 50 tasks per cycle  # fill-to-capacity + buffer, not a flat 30
DEFAULT_MIN_SCORE = 40  # Lowered for saturation penalty (injection -10, tfidf -5)  # Adapted for rescoring (exp_72526550): tighter distribution, percentile-based diminishing returns
DIVERSITY_CAP = 0.45  # Max 45% of tasks from same thread (raised from 0.35)
TRANSFER_CAP = 0.40   # Max 40% of tasks can be [TRANSFER] items (prevents queue flooding)

# RAG dedup — Layer 0.5: semantic check before creation
RAG_DEDUP_THRESHOLD = 0.95  # Raised from 0.90 — allow re-exploration of stranded findings (June 8 2026)
MAX_RAG_QUERIES = 100  # Cap RAG queries per cycle — at 1.1s each, 391 items = 430s (exceeds 300s timeout)

# Pre-check dedup cache — avoids spawning 132+ subprocesses
_PRECHECK_CACHE = {"loaded": False, "completed_hyps": None, "completed_results": None,
                   "running_titles": None, "phrase_index": None, "completed_types": None,
                   "timestamp": 0}
_PRECHECK_TTL = 120  # 2 minutes

# Pre-deployment entropy check (exp_72526101 / exp_101341)
# Flags high-difficulty tasks based on signal concentration metrics
ENTROPY_DIFFICULTY_THRESHOLD = 0.25  # Flag tasks above this difficulty score
ENTROPY_MODEL = "qwen2.5-0.5b"  # Lightweight model for entropy computation


def get_dynamic_running_threshold(free_count):
    """Scale running-task overlap threshold with worker availability.

    When many workers are idle, the cost of idle workers exceeds the cost
    of slight topic overlap between experiments. When workers are scarce,
    be conservative to avoid duplicates.

    Returns threshold for Layer 1 running-task overlap check.
    """
    if free_count > 20:
        return 0.75  # Many idle workers — loose (idle cost > overlap cost)
    elif free_count > 5:
        return 0.65  # Moderate — some caution
    else:
        return 0.60  # Few workers — conservative (current behavior)


def load_precheck_dedup_data():
    """Load pre-check dedup data once and cache it. Avoids 132+ subprocess spawns."""
    import time
    now = time.time()
    if (_PRECHECK_CACHE["loaded"] and
            now - _PRECHECK_CACHE["timestamp"] < _PRECHECK_TTL):
        return (_PRECHECK_CACHE["completed_hyps"], _PRECHECK_CACHE["completed_results"],
                _PRECHECK_CACHE["running_titles"], _PRECHECK_CACHE["phrase_index"],
                _PRECHECK_CACHE["completed_types"])

    sys.path.insert(0, str(SCRIPTS_DIR))
    from pre_check_dedup import load_completed_experiments, load_running_titles, build_phrase_index

    completed_hyps, completed_results, completed_types = load_completed_experiments()
    running_titles = load_running_titles()
    phrase_index = build_phrase_index(completed_hyps)

    _PRECHECK_CACHE.update({
        "loaded": True, "timestamp": now,
        "completed_hyps": completed_hyps, "completed_results": completed_results,
        "running_titles": running_titles, "phrase_index": phrase_index,
        "completed_types": completed_types,
    })
    return completed_hyps, completed_results, running_titles, phrase_index, completed_types


# Next experiment ID — ROOT-CAUSE FIX (2026-06-08): use the single shared
# allocator. Previously this did `max(int(digits))+1` over kanban, which (a) got
# polluted by 17-digit timestamp-style ids and (b) reset to exp_1/exp_2 (colliding
# with the oldest experiments) whenever the query saw a transiently empty/rebuilt
# table. The allocator anchors to the sequential series + a persisted high-water
# mark across BOTH dbs, so it is monotonic and cannot reset.
try:
    sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
    from exp_id_allocator import next_exp_id as _next_exp_id
    NEXT_EXP_ID = _next_exp_id()
except Exception:
    # Last-resort fallback: max sequential id across prometheus, never < 1.
    try:
        _conn = sqlite3.connect(os.path.expanduser("~/.hermes/prometheus.db"), timeout=5)
        _rows = _conn.execute("SELECT id FROM experiments").fetchall()
        _conn.close()
        _ids = [int(m.group(1)) for r in _rows
                if (m := re.search(r'exp_(\d+)', r[0])) and int(m.group(1)) < 1_000_000]
        NEXT_EXP_ID = max(_ids) + 1 if _ids else 1
    except Exception:
        NEXT_EXP_ID = 1


def classify_thread(text, source_domain=None):
    """Classify text into a research thread.
    
    Strips [TRANSFER] prefix before classifying — transfer items should be
    classified by their actual topic, not the prefix. This prevents all
    [TRANSFER] items from hitting the same thread cap.
    
    source_domain: if provided and text is a [TRANSFER] item, use this as
    fallback classification. This routes transfers back to the domain that
    generated them, creating bilateral edges instead of hub-only flows.
    """
    t = text.lower()
    # Strip [TRANSFER from domain] prefix to classify by actual content
    t_content = re.sub(r'\[transfer(?:\s+from\s+\w+)?\]\s*', '', t).strip()
    if any(kw in t_content for kw in ["tf-idf", "tfidf"]): return "tfidf"
    if any(kw in t_content for kw in ["lr", "logistic"]): return "lr_detection"
    if any(kw in t_content for kw in ["injection", "inject"]): return "injection"
    if any(kw in t_content for kw in ["calibration", "isotonic"]): return "calibration"
    if any(kw in t_content for kw in ["attack", "adversarial", "perturbation"]): return "attack"
    if any(kw in t_content for kw in ["hardware", "edge", "rpi", "fpga", "simd", "int1", "cascade"]): return "hardware"
    if any(kw in t_content for kw in ["domain", "multi-domain"]): return "domain"
    if any(kw in t_content for kw in ["embedding", "vector"]): return "embedding"
    if any(kw in t_content for kw in ["workspace", "cleanup", "ttl"]): return "workspace"
    if any(kw in t_content for kw in ["ensemble", "xgboost"]): return "ensemble"
    if any(kw in t_content for kw in ["consistency", "variance"]): return "consistency"
    if any(kw in t_content for kw in ["matrix"]): return "matrix_lr"
    if any(kw in t_content for kw in ["rag"]): return "rag"
    if any(kw in t_content for kw in ["synthesis", "consolidat"]): return "synthesis"
    if any(kw in t_content for kw in ["cognitive load", "experiment scheduling", "curiosity queue",
                                       "hypothesis generation", "confirmation bias", "diminishing returns",
                                       "meta-predictor", "agent's own", "agent learn", "agent predict",
                                       "research efficiency", "experiment fatigue", "cost-per-discovery"]): return "metacognition"
    # Fallback for [TRANSFER] items: route back to source domain if available
    # This creates bilateral edges instead of dumping everything into the hub
    if "[transfer]" in t:
        if source_domain:
            return source_domain  # injection_detection stays injection_detection
        return "cross_pollination"
    return "other"


def get_running_assignees():
    """Workers currently RUNNING a task (one in-progress per profile).

    Deep-queue model: we DO allow a worker to own several queued 'ready' tasks
    (the overflow buffer). The gateway enforces max_in_progress_per_profile=1,
    so those extra ready tasks just wait their turn. We only treat a worker as
    "busy" while it is actually running, for diagnostics — assignment itself
    (pick_worker) round-robins across the whole fleet regardless.

    Uses direct SQLite query instead of `hermes kanban list --json`
    which returns 37MB+ of JSON at scale (22K+ tasks).
    """
    try:
        db_path = os.path.expanduser("~/.hermes/kanban.db")
        db = sqlite3.connect(db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=3000")
        rows = db.execute(
            "SELECT assignee FROM tasks WHERE status='running' AND assignee IS NOT NULL"
        ).fetchall()
        db.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def get_running_titles():
    """Get titles of running tasks to avoid duplicates.
    
    Uses direct SQLite query instead of `hermes kanban list --json`
    which returns 37MB+ of JSON at scale (22K+ tasks).
    """
    try:
        db_path = os.path.expanduser("~/.hermes/kanban.db")
        db = sqlite3.connect(db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=3000")
        rows = db.execute(
            "SELECT title FROM tasks WHERE status IN ('running', 'ready', 'todo')"
        ).fetchall()
        db.close()
        return {r[0].lower() for r in rows}
    except Exception:
        return set()


def rag_dedup_check(hypothesis):
    """Query RAG to check if hypothesis has already been answered.
    
    Returns (is_duplicate, best_score, best_match_id) tuple.
    Returns (False, 0, None) if RAG server is unavailable or no match.
    This is Layer 0.5 of the dedup system — catches semantic duplicates
    that Jaccard word overlap misses.
    """
    try:
        # Import the RAG query function
        sys.path.insert(0, str(SCRIPTS_DIR))
        from experiment_rag import query_rag
        
        # Clean hypothesis for query
        query_text = re.sub(r'\[.*?\]\s*', '', hypothesis).strip()
        if len(query_text) < 10:
            return False, 0, None
        
        # Quick check: if hypothesis references a specific experiment ID,
        # check if that experiment already exists and has results.
        # NOTE: Only flag as duplicate if the question is ASKING about the same
        # thing the experiment found — NOT if it's asking about generalization.
        # "Does X generalize?" is a legitimate follow-up, not a duplicate.
        exp_ref_match = re.search(r'exp_(\d+)', hypothesis)
        if exp_ref_match:
            ref_id = f'exp_{exp_ref_match.group(1)}'
            try:
                db_path = os.path.expanduser("~/.hermes/prometheus.db")
                db = sqlite3.connect(db_path, timeout=3)
                db.execute("PRAGMA busy_timeout=2000")
                row = db.execute(
                    "SELECT id, hypothesis, result FROM experiments WHERE id = ? AND result IS NOT NULL",
                    (ref_id,)
                ).fetchone()
                db.close()
                if row and len(row[2]) > 20:
                    # Check if this is a genuine duplicate vs a follow-up question
                    # Follow-ups ask about GENERALIZATION/TRANSFER of a finding,
                    # not re-asking the same question. Key signals: "other", "beyond",
                    # "different", "across", "generalize", "extend", "transfer",
                    # "new", "also" — combined with an experiment reference.
                    follow_up_signals = ['generalize', 'extend', 'transfer',
                                        'other', 'beyond', 'different', 'across',
                                        'new domain', 'also', 'too']
                    hyp_lower = hypothesis.lower()
                    is_follow_up = any(sig in hyp_lower for sig in follow_up_signals)
                    if not is_follow_up:
                        # Genuine duplicate — same question, not a follow-up
                        return True, 0.95, ref_id
            except Exception:
                pass
        
        # Query RAG
        results = query_rag(query_text, top_k=3, worker_id="batch_dedup")
        
        if not results or (len(results) == 1 and "error" in results[0]):
            return False, 0, None
        
        # Check top result
        best = results[0]
        score = best.get("score", 0)
        doc_id = best.get("id", best.get("source_path", "unknown"))
        
        # If the matched document has no identifiable ID, it's likely a
        # topically-related document, not a completed experiment.
        # Don't treat as duplicate — can't verify what was matched.
        if doc_id == "unknown" or not doc_id:
            return False, score, doc_id
        
        if score >= RAG_DEDUP_THRESHOLD:
            return True, score, doc_id
        
        return False, score, doc_id
        
    except Exception:
        # Fail open — if RAG is unavailable, don't block task creation
        return False, 0, None


def entropy_preflight_check(item_text):
    """Pre-flight entropy check for a task item.
    
    Computes signal concentration metrics to predict fine-tuning difficulty.
    Low entropy = model knows the domain = easy to fine-tune.
    High entropy = diffuse distributions = harder to fine-tune.
    
    Returns (difficulty_score, should_flag, metrics_dict).
    difficulty_score: 0-1 where 0=easy, 1=hard
    should_flag: True if difficulty exceeds threshold
    metrics_dict: full metrics or None if computation failed
    
    Based on: exp_101341 (ρ=+1.0, p=0.000 for entropy predicting final loss)
    """
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from predeploy_entropy import compute_entropy_metrics, resolve_model_name
        
        # Extract the core question/prompt from the task text
        # Remove EXP_NNN prefix and BUILD/EXPERIMENT tags
        prompt = re.sub(r'^exp_\d+:\s*', '', item_text).strip()
        prompt = re.sub(r'\[.*?\]\s*', '', prompt).strip()
        if len(prompt) < 10:
            return 0.0, False, None
        
        metrics = compute_entropy_metrics(
            model_name=ENTROPY_MODEL,
            prompt=prompt,
            top_k=10,
            device="auto",
        )
        
        difficulty = metrics["difficulty_score"]
        should_flag = difficulty > ENTROPY_DIFFICULTY_THRESHOLD
        
        return difficulty, should_flag, metrics
        
    except Exception as e:
        # Fail open — don't block task creation on entropy computation failure
        print(f"  WARNING: Entropy check failed: {e}", file=sys.stderr)
        return 0.0, False, None


def score_queue_items():
    """Read pre-computed scores from DB (fast) instead of re-scoring (slow).
    
    Falls back to live scoring if DB scores are missing or stale (>30 min old).
    The background scorer (score_curiosities.py) writes combined_score using multi-objective model.
    """
    try:
        import sqlite3
        db_path = os.path.expanduser("~/.hermes/prometheus.db")
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        conn.row_factory = sqlite3.Row
        
        # Check if scores exist and are fresh
        # Use combined_score (multi-objective) if available, fall back to score
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN combined_score IS NOT NULL AND combined_score > 0 THEN 1 ELSE 0 END) as scored,
                   MAX(COALESCE(score_updated_at, created_at)) as last_update
            FROM curiosities WHERE status='active'
        """).fetchone()
        
        total = row["total"] or 0
        scored = row["scored"] or 0
        last_update = row["last_update"] or 0
        age_min = (time.time() - last_update) / 60 if last_update else 999
        
        # If scores are fresh (>50% scored and <30 min old), use them
        if scored > total * 0.5 and age_min < 30:
            rows = conn.execute("""
                SELECT text, priority, source_experiment,
                       combined_score as effective_score,
                       p_confirm, p_novel, p_expand, source_result_id
                FROM curiosities WHERE status='active'
                AND combined_score IS NOT NULL
                ORDER BY effective_score DESC
            """).fetchall()
            conn.close()
            
            items = []
            for r in rows:
                pri = r["priority"]
                # priority is INTEGER by convention but a handful of legacy
                # rows carry text labels ('high') — normalize instead of
                # crashing the whole scored-queue load on one bad row.
                if isinstance(pri, str):
                    try:
                        pri = int(pri)
                    except ValueError:
                        pri = {"high": 1, "medium": 3, "low": 5}.get(pri.lower())
                if pri is not None and pri <= 1:
                    pri_label = "high"
                elif pri is not None and pri <= 3:
                    pri_label = "medium"
                else:
                    pri_label = "low"
                items.append({
                    "text": r["text"] or "",
                    "source": r["source_experiment"] or "unknown",
                    "priority": pri_label,
                    "total": r["effective_score"] or 0,
                    "p_confirm": r["p_confirm"],
                    "p_novel": r["p_novel"],
                    "p_expand": r["p_expand"],
                    "source_result_id": r["source_result_id"],
                    "thread": "unknown",
                    "novelty": 0,
                    "diminishing": 0,
                    "source_quality": 0,
                    "diversity": 0,
                    "resolved": False,
                })
            return items
        
        # Fallback: no scores yet, return empty (merger will score on next insert)
        conn.close()
        return []
        print(f"Scores stale (scored={scored}/{total}, age={age_min:.0f}min), falling back to live scoring", file=sys.stderr)
        from curiosity_scorer import score_all, load_state
        state = load_state()
        return score_all(state)
        
    except Exception as e:
        print(f"ERROR: Could not score queue: {e}", file=sys.stderr)
        # Last resort fallback
        try:
            from curiosity_scorer import score_all, load_state
            state = load_state()
            return score_all(state)
        except Exception as e2:
            print(f"FATAL: Could not score queue: {e2}", file=sys.stderr)
            return []


def generate_task_body(item_text, source_exp=None, item_dict=None):
    """Generate a task body from a queue item description.
    
    If item_dict has experiment_type=ANALOGICAL, adds cross-domain transfer instructions.
    """
    # Clean up the item text
    # Parse JSON-encoded text from queue items (curiosity_scorer may return {"text": "..."})
    if item_text.startswith('{'):
        try:
            parsed = json.loads(item_text)
            if isinstance(parsed, dict) and 'text' in parsed:
                item_text = parsed['text']
        except (json.JSONDecodeError, ValueError):
            pass
    clean = re.sub(r"\[.*?\]\s*", "", item_text).strip()

    # ANALOGICAL-specific preamble
    is_analogical = item_dict and item_dict.get("experiment_type") == "ANALOGICAL"
    type_tag = ""
    type_instructions = ""
    if is_analogical:
        source_domain = item_dict.get("source_domain", "unknown")
        source_exp = item_dict.get("source", source_exp)
        mechanism_type = item_dict.get("mechanism_type", "EMPIRICAL_CORRELATION")
        mechanism_label = "universal law (first-principles math)" if mechanism_type == "UNIVERSAL_LAW" else "empirical correlation (fitted relationship)"
        type_tag = " [ANALOGICAL]"
        type_instructions = f"""
EXPERIMENT TYPE: ANALOGICAL — Cross-domain transfer experiment.
Source domain: {source_domain}
Source experiment: {source_exp}
Mechanism type: {mechanism_label}

This is NOT a standard hypothesis test. You are testing whether a mechanism
discovered in {source_domain} transfers to a new domain.
{"The source is a UNIVERSAL LAW — expect higher transfer success." if mechanism_type == "UNIVERSAL_LAW" else "The source is an EMPIRICAL CORRELATION — transfer may be contingent on specific conditions."}

APPROACH:
1. Identify the MECHANISM from the source experiment (not just the finding)
2. Determine if that mechanism's assumptions hold in the target domain
3. Design a test that would FAIL if the mechanism doesn't transfer
4. Run the test and report whether the mechanism generalizes

Do NOT just fit a model to synthetic data. The value is in the transfer,
not the confirmation.
"""

    body = f"""HYPOTHESIS: {clean}{type_tag}
{type_instructions}
HARD DEDUP GATE — DO NOT SKIP (52.7% of recent experiments were duplicates):
  Step 1: Query RAG
    python3 ~/.hermes/scripts/experiment_rag.py query "{clean[:80]}" --top-k 5 --worker-id worker

  Step 2: Check the top result's score
    - Score >= 0.80: STOP. This question has been answered.
      Write your result as a DUPLICATE and do NOT run the experiment:
      python3 ~/.hermes/scripts/write_worker_result.py \\
        --experiment exp_NNN \\
        --finding "DUPLICATE: already answered by <matched_exp_id>: <title from RAG>" \\
        --refuted \\
        --confidence 0.85 \\
        --domain <domain> \\
        --tags DUPLICATE,ALREADY_ANSWERED
      Then: kanban_complete with status=completed

    - Score 0.50-0.79: CAUTION. Check the matched result's title and hypothesis.
      If it asks the SAME question, treat as duplicate (score >= 0.60 = duplicate).
      If it asks a DIFFERENT question about the same topic, you may proceed.

    - Score < 0.50 or RAG server down: PROCEED with your experiment.

  Step 3: If you proceed, note in your result which past experiments you built on.

CONCRETENESS GATE (exp_72526588 — 50% waste from unclear results):
Before running, state a BINARY falsification criterion:
  - What specific result would SUPPORT your hypothesis?
  - What specific result would REFUTE it?
  If you cannot state both, reformulate the question until you can.
  Exploratory questions without binary outcomes waste compute (50.4% waste rate).

METHOD:
1. Design and run an experiment to investigate this question
2. Save results to ~/.hermes/experiments/ with descriptive filename
   CRITICAL: ALWAYS use absolute paths via os.path.expanduser("~/.hermes/experiments/")
   NEVER use relative paths — they resolve to your workspace, not ~/.hermes/experiments/
   NEVER write to the home directory itself, not a workspace
   Example: output_path = os.path.expanduser("~/.hermes/exp**_results.json")
3. Report findings with evidence
4. In your result, state which past experiments you built on (if any)

GPU AVAILABLE: Local RTX 5090 (32GB VRAM) accessible via `gpu_run` CLI.
- Use for: local model testing, batch processing, embeddings, fine-tuning
- Check status: `gpu_run status`
- Inference: `gpu_run inference --model qwen2.5-0.5b --prompt "..."`
- Batch: `gpu_run inference --model qwen2.5-7b --input prompts.jsonl --output results.jsonl`
- Embeddings: `gpu_run embedding --model qwen3-embedding --input docs.jsonl --output embeddings.npy`  (1024-d, via the shared :9150 server)
- Models auto-download on first use. Load `gpu-toolset` skill for full docs.
- Use GPU when: testing locally saves API costs, batch jobs, or experiments need fast iteration
- GPU is shared: check `gpu_run status` before requesting. Don't hog if others need it.

TORCH ON GPU: When writing torch/ML code, ALWAYS use:
  `DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')`
  Do NOT hardcode `torch.device('cpu')` — the GPU is available and should be used.
  Move models and tensors to DEVICE with `.to(DEVICE)`.

GPU SKLEARN: Automatic — transparent import hook redirects sklearn to GPU.
  Standard sklearn imports (LogisticRegression, PCA, StandardScaler, etc.)
  are automatically GPU-accelerated. No code changes needed.
  Crossover: >5K samples with >50 features. Below that, CPU is faster.

CRITICAL RULES:
- Workers have FULL terminal access — use it to run local scripts like write_worker_result.py
- Workers have read_file, write_file, patch, search_files, terminal, process tools
- Workers NEVER SSH to remote infrastructure (but local terminal is available)
- Workers NEVER modify inference servers
- Read OPENROUTER_API_KEY from ~/.hermes/.env (KEY_PREFIX concatenation to avoid TIRITH filter)
- Temperature: 0.3, max_tokens: 4096 per API call
- Use individual calls per question (not batch — batch inflates accuracy for reasoning models)
- mimo-v2.5 is a REASONING MODEL: content=null means answers are in reasoning field

PREREGISTER (MANDATORY — commit to this BEFORE writing any experiment code):
State, in your very first working note, what you expect this experiment to show:
  PREDICTION: SUPPORTED or REFUTED
  CONFIDENCE: 0.0-1.0 (how strongly your prior expects it)
  WHY: one line — what prior knowledge drives the prediction
Then run the experiment. NEVER revise the prediction after seeing results.
A wrong prediction with an honest result is MORE valuable than a correct one —
it means the experiment carried information your prior did not have. Prediction
accuracy is scored system-wide (prior-override audit); report it via
--predicted-direction below as {{"<hypothesis_key>": +1|-1}} (+1=SUPPORTED,
-1=REFUTED; reuse the key in --observed-direction), even (especially) wrong.

RESULT WRITING (MANDATORY — do this BEFORE kanban_complete):
After completing the experiment, write a structured result:
python3 ~/.hermes/scripts/write_worker_result.py \
  --experiment exp_NNN \
  --finding "WHAT you found AND WHY it works (mechanism)" \
  --supported \\\\          # ONLY if finding starts with CONFIRMED/SUPPORTED
                            # Use --refuted if finding starts with REFUTED
                            # The finding text is the source of truth — flag must match it
  --confidence 0.85 \\      # HARD CAP: a single worker result may NOT exceed 0.85.
                            # 0.85 already means "high confidence"; the system aggregates
                            # across independent workers for anything higher. Values >0.85
                            # are REJECTED and you'll have to re-run this command. When unsure,
                            # go LOWER — overconfident single results are empirically LESS accurate.
  --domain <domain> \
  --tags CONFIRMED,surprise \
  --files "exp_NNN.py,exp_NNN_results.json" \
  --predicted-direction '{{"<hypothesis_key>":1}}' \
  --queue "Follow-up question 1?;[TRANSFER from source_domain] Cross-domain question?"
The --finding should include the MECHANISM (WHY), not just the verdict.
Good: "CONFIRMED: F1=0.95 BECAUSE adversarial inputs cluster in low-dim subspace"
Bad:  "CONFIRMED: F1=0.95"
The mechanism enables cross-domain transfer — synthesis uses it to ask
"where else does this mechanism apply?"
Tag cross-domain transfer questions with [TRANSFER] in --queue.
This updates the experiments table and self_state.json directly.
Do NOT skip this step — it is the primary path for results to enter the knowledge base.

EXPLORATORY RESULTS (NEVER BLOCK — write instead):
If your result is INCONCLUSIVE, mixed, weak, or your confidence is below 0.5:
  DO NOT BLOCK the task for human review.
  INSTEAD, write the result as EXPLORATORY:
python3 ~/.hermes/scripts/write_worker_result.py \
  --experiment exp_NNN \
  --finding "INCONCLUSIVE: [what you found, including the weak signals]" \
  --confidence 0.3 \
  --tags EXPLORATORY \
  --type ANALOGICAL \
  --kanban-task <task_id>
This enters the knowledge base as a low-confidence signal. Synthesis and
future experiments can corroborate or refute it. Blocking wastes a worker
cycle and requires human intervention for something the system can track
itself. Only block if the experiment CRASHED or produced NO DATA AT ALL.

METRICS LOGGING (MANDATORY — do this AFTER the experiment, BEFORE kanban_complete):
Log cognitive load metrics for real-time monitoring (exp_72526579):
python3 ~/.hermes/scripts/log_experiment_metrics.py --experiment exp_NNN --finish
This logs latency, token rate, and complexity classification.
High latency ratio (>5x) or complex classification = flag for routing review."""

    # Auto-detect GPU-capable operations and inject specific instructions
    if GPU_ROUTE_AVAILABLE:
        ops = detect_ops(clean)
        if ops:
            gpu_instructions = generate_gpu_instructions(ops)
            # Insert before CRITICAL RULES
            marker = "CRITICAL RULES:"
            if marker in body:
                parts = body.split(marker, 1)
                body = parts[0] + gpu_instructions + "\n" + marker + parts[1]

    # KNOWLEDGE FEED (parity with task_refiller.py enrichment): bake related
    # CONFIRMED/SUPPORTED prior findings into the card body so the worker
    # builds on them instead of re-deriving. Same filter as the refiller:
    # positive verdicts only, RAG score > 0.5, self-negating text excluded,
    # top 3. Fails open — any problem leaves the body as-is.
    # suppress_prior_context (2026-07-04): epistemic lanes (retest/boundary/
    # clean-room) run BLIND — same guard as the refiller.
    try:
        from prior_feed_stamp import suppress_prior_context as _suppress_fn
        _suppress_prior = _suppress_fn(clean)
    except Exception:
        _suppress_prior = False
    if clean and len(clean) > 10 and not _suppress_prior:
        try:
            from experiment_rag import query_rag
            _rag_results = query_rag(clean, top_k=6, worker_id="batch_enrichment")
            if _rag_results and not isinstance(_rag_results, dict):
                _parts = []
                for _r in _rag_results:
                    if _r.get("score", 0) <= 0.5:
                        continue
                    _res = (_r.get("result") or "").strip()
                    _resU = _res.upper()
                    if not (_resU.startswith("CONFIRMED") or _resU.startswith("SUPPORTED")):
                        continue
                    if " REFUTED" in _resU[:60] or "DOES NOT" in _resU[:60]:
                        continue
                    _finding = _res[:180] if _res else (_r.get("hypothesis") or "")[:120]
                    if _finding:
                        _parts.append(f"- [{_r.get('score', 0):.2f}] {_finding}")
                    if len(_parts) >= 3:
                        break
                if _parts:
                    body += (
                        "\n\nCONFIRMED PRIOR FINDINGS (build on these, do not re-test; "
                        "they have NOT all survived independent replication, so treat as "
                        "strong priors, not ground truth):\n" + "\n".join(_parts)
                    )
        except Exception:
            pass

    return body


def pick_worker(free_workers, used_workers):
    """Assign this ready task to a worker, round-robin across the WHOLE fleet.

    Deep-queue model: tasks must be pre-assigned (the gateway only spawns ready
    tasks that have an assignee). We balance assignment so each worker gets a
    similar-size queue: prefer workers not yet used in THIS batch; once every
    worker has one, wrap around and stack a second, etc. The gateway runs one
    task per worker at a time (max_in_progress_per_profile=1); the rest wait as
    the deep 'ready' overflow. Never returns None — we always want to stage more.
    """
    fleet = list(WORKERS)
    if not fleet:
        return None
    fresh = [w for w in fleet if w not in (used_workers or set())]
    return random.choice(fresh) if fresh else random.choice(fleet)


def create_task(title, assignee, body, priority=None):
    """Create a Kanban task via safe_kanban_create.py (dedup-gated).

    assignee=None creates an UNASSIGNED 'ready' task (backlog) which the
    kernel dispatcher claims for a worker when one frees up.

    priority: kanban dispatch priority (higher = dispatched first). When None,
    the task lands at the DB default (0). The normal dedup pipeline computes
    this via get_task_priority so injection/transfer/exploration tasks created
    here get the same tiering as the fast-path refiller, instead of silently
    defaulting to P0.
    """
    safe_create = str(SCRIPTS_DIR / "safe_kanban_create.py")
    cmd = [sys.executable, safe_create, title]
    if assignee:
        cmd += ["--assignee", assignee]
    if priority is not None:
        cmd += ["--priority", str(priority)]
    cmd += ["--body", body]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            match = re.search(r"t_[a-f0-9]+", r.stdout)
            tid = match.group(0) if match else None
            if tid:
                # Durable prior-fed stamp (independence gate) — best-effort, keyed by
                # task id in prometheus.db so it survives kanban body archival.
                try:
                    from prior_feed_stamp import record as _stamp_prior_feed
                    _stamp_prior_feed(tid, body)
                except Exception:
                    pass
            return tid if tid else "created"
        elif r.returncode == 1:
            # Dedup gate blocked it
            return None
        else:
            return None
    except Exception as e:
        print(f"  ERROR creating task: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description="Batch Task Creator")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT,
                        help=f"Max tasks to create (default: {DEFAULT_COUNT})")
    parser.add_argument("--min-score", type=int, default=DEFAULT_MIN_SCORE,
                        help=f"Minimum score threshold (default: {DEFAULT_MIN_SCORE})")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--entropy-check", action="store_true",
                        help="Run pre-deployment entropy check on selected tasks (flags high-difficulty)")
    parser.add_argument("--entropy-threshold", type=float, default=ENTROPY_DIFFICULTY_THRESHOLD,
                        help=f"Difficulty threshold for entropy flagging (default: {ENTROPY_DIFFICULTY_THRESHOLD})")
    parser.add_argument("--only", choices=["genuine", "transfer"], default=None,
                        help="Lane filter: 'genuine' = only non-transfer experiment ideas; "
                             "'transfer' = only [TRANSFER]/ANALOGICAL items. Omit to mix both. "
                             "Used by the Director to run reserved-slot lanes so genuine "
                             "experiments and transfers no longer compete for the same slots.")
    args = parser.parse_args()

    if args.count <= 0:
        print("Nothing to create (count=0)")
        return

    # --- RAPID-FILL MODE ---
    # If the ready queue is nearly empty, skip expensive dedup and create tasks
    # directly from the scored queue. 5% duplicate waste is acceptable when
    # 50 workers are sitting idle. Dedup becomes the bottleneck at scale.
    RAPID_FILL_THRESHOLD = 50  # Match worker count — keep all 50 workers fed
    try:
        import sqlite3 as _rapid_sqlite
        _rapid_db = _rapid_sqlite.connect(os.path.expanduser("~/.hermes/kanban.db"), timeout=5)
        _ready_count = _rapid_db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='ready'"
        ).fetchone()[0]
        _rapid_db.close()
    except Exception:
        _ready_count = RAPID_FILL_THRESHOLD  # assume not rapid-fill if check fails

    if _ready_count < RAPID_FILL_THRESHOLD:
        # Fast path: score queue, skip all dedup layers, create directly via DB
        print(f"RAPID-FILL MODE: {_ready_count} ready tasks, skipping dedup", flush=True)
        scored = score_queue_items()
        active = [s for s in scored if not s.get("resolved", False) and s["total"] >= 30]
        
        # EXPLORATION BUDGET POLICY LAYER (confirmation-weighted)
        # Boost curiosities targeting under-evidenced domains
        # Uses confirmed experiments as "effective evidence" instead of raw count
        # This is POLICY, not prediction. Predictor says what WILL happen.
        # Policy says what we WANT to happen.
        try:
            import sqlite3 as _pol_sqlite, re as _pol_re
            _pol_db = _pol_sqlite.connect(os.path.expanduser("~/.hermes/prometheus.db"), timeout=5)
            # Get confirmation-weighted evidence per domain
            _domain_evidence = {}
            for _row in _pol_db.execute("""
                SELECT e.domain,
                       SUM(CASE WHEN wr.key_finding LIKE '%CONFIRMED%' OR wr.key_finding LIKE '%SUPPORTED%' THEN 1 ELSE 0 END) as confirmed
                FROM experiments e
                LEFT JOIN worker_results wr ON e.id = wr.experiment_id
                WHERE e.domain IS NOT NULL
                GROUP BY e.domain
            """).fetchall():
                _domain_evidence[_row[0]] = _row[1] or 0
            _pol_db.close()
            
            _EVIDENCE_THRESHOLD = 8  # Graduate when 8+ confirmed experiments
            for _item in active:
                _text = (_item.get("text") or "").lower()
                # Find target domain in text
                _match = _pol_re.search(r'\[transfer\s+from\s+(\w+)\s*(?:→|to)\s*(\w+)\]', _text)
                _target = _match.group(2) if _match else None
                if not _target:
                    # Try simpler pattern: domain mentioned after "apply to" or "in"
                    _match2 = _pol_re.search(r'(?:apply|transfer|extend)\s+(?:to|in)\s+(\w+)', _text)
                    _target = _match2.group(1) if _match2 else None
                
                if _target and _target in _domain_evidence:
                    _evidence = _domain_evidence[_target]
                    if _evidence < _EVIDENCE_THRESHOLD:
                        # Boost proportional to evidence deficit
                        _deficit = _EVIDENCE_THRESHOLD - _evidence
                        _boost = min(15, 5 + _deficit * 2)  # 5-15 points based on deficit
                        _item["total"] = _item.get("total", 0) + _boost
                        _item["exploration_boost"] = True
        except Exception:
            pass
        # Lane filter
        if args.only == "genuine":
            active = [s for s in active if "[transfer" not in (s.get("text", "") or "").lower()]
        elif args.only == "transfer":
            active = [s for s in active if "[transfer" in (s.get("text", "") or "").lower()]
        active.sort(key=lambda s: -s["total"])
        to_create = active[:args.count]
        if not to_create:
            print("No items to create in rapid-fill mode")
            return
        # Create tasks directly via DB — bypass safe_kanban_create dedup
        import uuid as _rapid_uuid
        running = get_running_assignees()
        all_workers = ["default"]
        free = [w for w in all_workers if w not in running]
        _created = 0
        _kanban = sqlite3.connect(os.path.expanduser("~/.hermes/kanban.db"), timeout=10)
        for s in to_create:
            if not free:
                break
            title = s.get("text", "")[:80]
            body = generate_task_body(s.get("text", ""))
            worker = pick_worker(free, [])
            if worker:
                tid = "t_" + _rapid_uuid.uuid4().hex[:8]
                _priority = get_task_priority(s, "genuine")
                _kanban.execute(
                    "INSERT INTO tasks (id, title, body, assignee, status, priority, created_at, goal_mode, goal_max_turns, skills) "
                    "VALUES (?, ?, ?, ?, 'ready', ?, ?, 1, 6, '[\"kanban-worker\"]')",
                    (tid, title, body, worker, _priority, int(time.time()))
                )
                _created += 1
        _kanban.commit()
        _kanban.close()
        print(f"Rapid-fill created {_created}/{len(to_create)} tasks (no dedup)")
        return

    # --- NORMAL MODE (dedup pipeline below) ---

    # Get scored queue items
    scored = score_queue_items()
    active = [s for s in scored if not s.get("resolved", False)]

    # Filter by score
    high_value = [s for s in active if s["total"] >= args.min_score]

    # Lane filter (--only): split the pipeline so transfers and genuine
    # experiments don't fight for the same slots. Transfers bypass several
    # dedup layers (see is_analogical below), which at semantic saturation
    # lets them crowd out genuine experiments. Running two reserved lanes
    # (e.g. 18 genuine + 12 transfer) makes the mix an explicit dial.
    def _is_transfer_item(s):
        if not isinstance(s, dict):
            return False
        if s.get("experiment_type") == "ANALOGICAL":
            return True
        return "[TRANSFER" in (s.get("text", "") or "")
    if args.only == "genuine":
        high_value = [s for s in high_value if not _is_transfer_item(s)]
    elif args.only == "transfer":
        high_value = [s for s in high_value if _is_transfer_item(s)]
    if args.only and not args.json:
        print(f"LANE FILTER: --only {args.only} -> {len(high_value)} candidates after lane filter")

    # Get current state
    running_assignees = get_running_assignees()
    running_titles = get_running_titles()
    free_workers = set(WORKERS) - running_assignees

    # Dynamic threshold: scale running-task overlap check with worker availability
    running_threshold = get_dynamic_running_threshold(len(free_workers))

    # Load completed experiment hypotheses AND results for deduplication
    # Use prometheus.db as primary source (richer data), fall back to self_state.json
    completed_hyps = {}
    completed_results = {}
    try:
        db_path = os.path.expanduser("~/.hermes/prometheus.db")
        db = sqlite3.connect(db_path, timeout=5)
        db.execute("PRAGMA busy_timeout=3000")
        rows = db.execute(
            "SELECT id, hypothesis, result FROM experiments WHERE status='completed'"
        ).fetchall()
        db.close()
        for eid, hyp, res in rows:
            if hyp and len(hyp) > 20:
                completed_hyps[eid] = hyp.lower()
            if res and len(res) > 20:
                completed_results[eid] = res.lower()[:500]
    except Exception:
        # Fallback to self_state.json
        try:
            with open(SELF_STATE_PATH) as f:
                ss = json.load(f)
            for exp in ss.get("experiments", {}).get("completed", []):
                if isinstance(exp, dict):
                    eid = exp.get("id", "")
                    if exp.get("hypothesis"):
                        completed_hyps[eid] = exp["hypothesis"].lower()
                    if exp.get("result"):
                        completed_results[eid] = exp["result"].lower()[:500]
                elif isinstance(exp, str) and exp.startswith("exp_"):
                    # Handle string format: "exp_NNN: description..."
                    parts = exp.split(": ", 1)
                    if len(parts) == 2:
                        eid = parts[0].strip()
                        desc = parts[1].lower()
                        completed_hyps[eid] = desc
                        completed_results[eid] = desc[:500]
        except Exception:
            pass

    # Filter out items already covered by running tasks OR completed experiments
    # NOTE: Thresholds tightened June 2026 — 49x duplicate "defense-depth" found
    # Old: hyp 0.45, result 0.55, running 0.45, intra-batch 0.55
    # New: hyp 0.65, result 0.65, running dynamic (0.60-0.75), intra-batch 0.65
    
    # Phrase-level dedup: extract key phrases and check for semantic matches
    # This catches duplicates that rephrase the same question with different words
    STOPWORDS = {"does", "that", "this", "with", "from", "have", "been", "will",
                 "more", "than", "when", "what", "than", "also", "only", "other",
                 "into", "over", "such", "after", "before", "about", "would", "could",
                 "should", "might", "being", "there", "their", "which", "these", "those"}
    
    def extract_key_phrases(text):
        """Extract distinctive 2-word phrases from text."""
        words = re.findall(r'\w{3,}', text.lower())
        phrases = set()
        for i in range(len(words) - 1):
            if words[i] not in STOPWORDS and words[i+1] not in STOPWORDS:
                phrases.add(f"{words[i]} {words[i+1]}")
        return phrases
    
    def phrase_overlap(item_phrases, hyp_text):
        """Check how many key phrases from item appear in hypothesis."""
        if not item_phrases:
            return 0.0
        hyp_lower = hyp_text.lower()
        matches = sum(1 for p in item_phrases if p in hyp_lower)
        return matches / max(len(item_phrases), 1)
    
    # Pre-build phrase index for completed hypotheses (for fast lookup)
    hyp_phrase_index = {}  # phrase -> list of exp_ids
    for exp_id, hyp in completed_hyps.items():
        phrases = extract_key_phrases(hyp)
        for p in phrases:
            if p not in hyp_phrase_index:
                hyp_phrase_index[p] = []
            hyp_phrase_index[p].append(exp_id)
    
    uncovered = []
    rag_dedup_hits = 0
    rag_dedup_misses = 0
    rag_queries_run = 0  # Cap RAG queries to stay under timeout

    # --- BATCH FAISS DEDUP (Phase 2 optimization) ---
    # Pre-compute RAG dedup results for ALL items at once using FAISS index.
    # This replaces sequential rag_dedup_check() calls (0.6s each → 9+ min total)
    # with a single batch query (~0.3s for 400 items).
    _faiss_dedup_results = {}  # first_question -> (is_dup, score, match_id)
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from faiss_dedup import get_index as _faiss_get_index
        from faiss_dedup import check_duplicates_batch as _faiss_batch_check
        _faiss_index, _faiss_exp_ids = _faiss_get_index()
        _all_questions = []
        _question_to_idx = {}
        for idx, item in enumerate(high_value):
            raw_text = item["text"]
            fq = raw_text.split(";")[0].strip() or raw_text
            _all_questions.append(fq)
            _question_to_idx[idx] = len(_all_questions) - 1
        if _all_questions:
            _faiss_results = _faiss_batch_check(_all_questions, _faiss_index, _faiss_exp_ids)
            for idx, result in enumerate(_faiss_results):
                fq = _all_questions[idx] if idx < len(_all_questions) else None
                if fq:
                    _faiss_dedup_results[fq] = (result["is_dup"], result["score"], result["match_id"])
        print(f"FAISS batch dedup: {len(_faiss_dedup_results)} items pre-checked", flush=True)
    except Exception as e:
        print(f"FAISS dedup unavailable, falling back to sequential: {e}", flush=True)
        _faiss_dedup_results = {}

    for item in high_value:
        # safe_kanban_create only sees the FIRST question (before ';'), so
        # dedup must check only that question. The full semicolon-separated
        # text inflates phrase counts and drops overlap ratios below thresholds,
        # causing batch_create to falsely report items as "uncovered".
        raw_text = item["text"]
        first_question = raw_text.split(";")[0].strip()
        if not first_question:
            first_question = raw_text

        text_lower = first_question.lower()
        item_words = set(re.findall(r"\\w{4,}", text_lower))
        item_phrases = extract_key_phrases(text_lower)

        # ANALOGICAL items are cross-domain transfers — they SHOULD share
        # vocabulary with their source experiments. Skip word-overlap dedup
        # layers (1-4) but keep RAG semantic check (layer 0.5).
        # Also treat [TRANSFER] items as analogical — they are cross-domain
        # hypotheses generated by synthesis that should bypass word-overlap dedup.
        is_analogical = (
            isinstance(item, dict) and
            (item.get("experiment_type") == "ANALOGICAL" or
             "[TRANSFER" in item.get("text", ""))
        )

        # Layer 0.5: RAG semantic dedup — use pre-computed FAISS results
        is_covered = False
        rag_is_dup, rag_score, rag_match = False, 0, None
        if first_question in _faiss_dedup_results:
            rag_is_dup, rag_score, rag_match = _faiss_dedup_results[first_question]
        elif rag_queries_run < MAX_RAG_QUERIES:
            # Fallback: sequential check for items not in batch
            rag_is_dup, rag_score, rag_match = rag_dedup_check(first_question)
            rag_queries_run += 1
        if rag_is_dup:
            is_covered = True
            rag_dedup_hits += 1
            if not args.json:
                print(f"  RAG DEDUP: [{rag_score:.3f}] {first_question[:60]}")
                print(f"            → matches {rag_match}")
        else:
            rag_dedup_misses += 1

        # Layer 0.7: pre_check_dedup.py alignment — match safe_kanban_create's dedup
        # OPTIMIZATION: Skip for items where RAG score < 0.5 (clearly novel).
        # RAG catches semantic duplicates via embedding similarity; if RAG says
        # novel, phrase overlap won't find a duplicate RAG missed. This saves
        # ~0.3s per item × 200+ items = 60s+ per cycle. Also skip for
        # ANALOGICAL items which are exempt from most dedup layers.
        if not is_covered and len(first_question) >= 20 and not is_analogical:
            # Only run pre_check if RAG score is borderline (>= 0.5) or no RAG score
            rag_is_borderline = rag_score >= 0.5
            if rag_is_borderline or rag_score == 0:
                try:
                    # In-process dedup — loads data once, reuses for all items
                    completed_hyps, completed_results, running_titles, phrase_index, completed_types = load_precheck_dedup_data()
                    sys.path.insert(0, str(SCRIPTS_DIR))
                    from pre_check_dedup import check_duplicate
                    result = check_duplicate(first_question, completed_hyps, completed_results,
                                             running_titles, phrase_index,
                                             running_threshold=running_threshold,
                                             completed_types=completed_types)
                    if result.get("is_duplicate"):
                        is_covered = True
                        matches = result.get("matches", [])
                        if matches and not args.json:
                            best = matches[0]
                            print(f"  PRE_CHECK DEDUP: [{best.get('score', '?')}] {first_question[:60]}")
                            print(f"            → matches {best.get('exp_id', '?')} (method={best.get('method', '?')})")
                except Exception as e:
                    print(f"  PRE_CHECK DEDUP ERROR: {e}", file=sys.stderr)
                    pass

        # Layer 1: Check if any running task covers this topic
        # ALWAYS check — running tasks are active duplicates regardless of type
        if not is_covered:
            for rt in running_titles:
                words2 = set(re.findall(r"\w{4,}", rt))
                if item_words and words2:
                    overlap = len(item_words & words2) / max(len(item_words | words2), 1)
                    if overlap > running_threshold:
                        is_covered = True
                        break

        # Layer 2: Check if any completed experiment already answered this question
        # SKIP for ANALOGICAL — transfers are NEW questions derived from completed work
        if not is_covered and not is_analogical:
            for exp_id, hyp in completed_hyps.items():
                hyp_words = set(re.findall(r"\w{4,}", hyp))
                if item_words and hyp_words:
                    overlap = len(item_words & hyp_words) / max(len(item_words | hyp_words), 1)
                    if overlap > 0.65:
                        is_covered = True
                        break
        # Also check against completed experiment RESULTS
        # SKIP for ANALOGICAL — same reason as Layer 2
        if not is_covered and not is_analogical:
            for exp_id, res in completed_results.items():
                res_words = set(re.findall(r"\w{4,}", res))
                if item_words and res_words:
                    overlap = len(item_words & res_words) / max(len(item_words | res_words), 1)
                    if overlap > 0.65:
                        is_covered = True
                        break
        # Layer 3: Phrase-level dedup — catches semantic rephrases
        # SKIP for ANALOGICAL — phrases overlap with source by design
        if not is_covered and not is_analogical and item_phrases:
            # Find candidate hyps that share at least 2 phrases with the item
            candidate_counts = Counter()
            for phrase in item_phrases:
                if phrase in hyp_phrase_index:
                    for exp_id in hyp_phrase_index[phrase]:
                        candidate_counts[exp_id] += 1
            for exp_id, count in candidate_counts.most_common(5):
                if count >= 2 and exp_id in completed_hyps:
                    ratio = count / max(len(item_phrases), 1)
                    if ratio > 0.30:  # 30%+ of key phrases match
                        is_covered = True
                        break

        # Layer 4: Novelty gate — reject hypotheses too similar to recent experiments
        # (exp_72526597: 75.2% confirmation bias from designing experiments around known methods)
        # SKIP for ANALOGICAL — cross-domain novelty is the whole point
        if not is_covered and not is_analogical and item_words:
            recent_hyps = list(completed_hyps.values())[-50:]  # Last 50 experiments
            for hyp in recent_hyps:
                hyp_words = set(re.findall(r"\w{4,}", hyp))
                if hyp_words:
                    overlap = len(item_words & hyp_words) / max(len(item_words | hyp_words), 1)
                    if overlap > 0.70:
                        is_covered = True
                        break

        if not is_covered:
            uncovered.append(item)
            # OPTIMIZATION: Early exit once we have enough candidates.
            # We need `count` tasks, but diversity cap + intra-batch dedup
            # may reduce selection. Collect 3x buffer, then stop dedupping.
            if len(uncovered) >= args.count * 3:
                break

    # Load governance statuses for enforcement
    try:
        import sqlite3 as _sql
        _gdb = _sql.connect(os.path.expanduser("~/.hermes/prometheus.db"), timeout=5)
        _gdb.execute("PRAGMA busy_timeout=3000")
        _grows = _gdb.execute("SELECT claim_text, status FROM architectural_claims").fetchall()
        _gdb.close()
        _governance_statuses = {}
        for _gt, _gs in _grows:
            _gl = _gt.lower()
            if "transfer" in _gl and ("track" in _gl or "table" in _gl):
                _governance_statuses["transfer_tracking"] = {"status": _gs}
            elif "novelty" in _gl and "scor" in _gl:
                _governance_statuses["novelty"] = {"status": _gs}
            elif "domain" in _gl and "taxonomy" in _gl:
                _governance_statuses["domain_taxonomy"] = {"status": _gs}
    except Exception:
        _governance_statuses = {}  # registry unavailable — no enforcement

    # Apply diversity cap
    thread_counts = Counter()
    selected = []
    max_per_thread = max(1, int(args.count * DIVERSITY_CAP))
    # Adaptive transfer cap: scales up when backlog is deep
    if TRANSFER_TRACKING_AVAILABLE:
        transfer_backlog = get_transfer_backlog()
        TRANSFER_CAP_EFFECTIVE = compute_adaptive_transfer_cap(transfer_backlog)
        print(f"Transfer backlog: {transfer_backlog}, adaptive cap: {TRANSFER_CAP_EFFECTIVE:.2f}")
    else:
        TRANSFER_CAP_EFFECTIVE = TRANSFER_CAP
    max_transfers = max(1, int(args.count * TRANSFER_CAP_EFFECTIVE))
    transfer_count = 0

    for item in uncovered:
        thread = item["thread"]
        if thread == "synthesis":
            continue  # Don't batch-create synthesis tasks
        if thread_counts[thread] >= max_per_thread:
            continue
        # Transfer cap: limit % of tasks that are cross-domain transfers
        is_transfer = "[transfer" in (item.get("text", "") or "").lower()
        if is_transfer and transfer_count >= max_transfers:
            continue
        # GOVERNANCE ENFORCEMENT: block items routing through downgraded subsystems
        if is_transfer and _governance_statuses.get("transfer_tracking", {}).get("status") == "downgraded":
            continue  # transfer_tracking downgraded — do not create transfer tasks
        if len(selected) >= args.count:
            break
        # NOTE: We intentionally do NOT clamp to len(free_workers) here.
        # Creating more tasks than currently-idle workers builds a 'ready'
        # backlog (the buffer) that the kernel dispatcher drains as workers
        # finish — this is what keeps a stack of work always waiting instead
        # of the board draining to zero between cycles. The lane cap
        # (args.count, derived from worker_config.cycle_target) bounds it.
        # Intra-batch dedup: skip if any already-selected item has >0.55 overlap
        item_words = set(re.findall(r"\w{4,}", item["text"].lower()))
        is_dup = False
        for sel in selected:
            sel_words = set(re.findall(r"\w{4,}", sel["text"].lower()))
            if item_words and sel_words:
                overlap = len(item_words & sel_words) / max(len(item_words | sel_words), 1)
                if overlap > 0.65:
                    is_dup = True
                    break
        if is_dup:
            continue
        selected.append(item)
        thread_counts[thread] += 1
        if is_transfer:
            transfer_count += 1

    next_id = [NEXT_EXP_ID]  # mutable container for closure

    if args.json:
        result = {
            "total_scored": len(active),
            "high_value": len(high_value),
            "rag_dedup_hits": rag_dedup_hits,
            "rag_dedup_misses": rag_dedup_misses,
            "rag_queries_run": rag_queries_run,
            "rag_queries_cap": MAX_RAG_QUERIES,
            "uncovered": len(uncovered),
            "free_workers": len(free_workers),
            "selected": len(selected),
            "tasks": [{
                "title": f"exp_{next_id[0] + idx}: " + re.sub(r'\[TRANSFER.*?\]\s*[^;]*;?\s*', '', re.sub(r'\[.*?\]\s*', '', re.sub(r'\[\[', '[', (lambda t: json.loads(t).get('text', t) if t.startswith('{') else t)(s['text'])))).strip()[:80],
                "thread": s["thread"],
                "score": s["total"],
                "body": generate_task_body(s["text"], item_dict=s),
            } for idx, s in enumerate(selected)]
        }
        print(json.dumps(result, indent=2))
        return

    # Print plan
    print(f"Queue: {len(active)} active items, {len(high_value)} score >= {args.min_score}")
    print(f"Workers: {len(free_workers)} free, {len(running_assignees)} busy")
    print(f"Running-task threshold: {running_threshold} ({len(free_workers)} free workers)")
    print(f"RAG dedup: {rag_dedup_hits} caught, {rag_dedup_misses} passed (threshold: {RAG_DEDUP_THRESHOLD}, queries: {rag_queries_run}/{MAX_RAG_QUERIES})")
    print(f"Uncovered by all dedup layers: {len(uncovered)}")
    print(f"Selected: {len(selected)} tasks (diversity cap: {max_per_thread}/thread, transfer cap: {max_transfers})")
    print()

    if not selected:
        print("No tasks to create.")
        return

    # Thread distribution
    dist = Counter(s["thread"] for s in selected)
    print("Thread distribution:")
    for thread, count in dist.most_common():
        print(f"  {thread}: {count}")
    print()

    if args.dry_run:
        print("DRY RUN — tasks that would be created:")
        dry_used = set()
        entropy_flagged = 0
        for i, s in enumerate(selected):
            worker = pick_worker(free_workers, dry_used)
            if worker:
                dry_used.add(worker)
            entropy_info = ""
            if args.entropy_check:
                diff, flagged, _ = entropy_preflight_check(s["text"])
                if flagged:
                    entropy_flagged += 1
                    entropy_info = f" ⚠ ENTROPY={diff:.3f} (>{args.entropy_threshold})"
                else:
                    entropy_info = f" entropy={diff:.3f}"
            print(f"  {i+1:2d}. [{int(s['total']):3d}] [{s['thread']:12s}] → {worker or 'NO WORKER'}{entropy_info}")
            print(f"      {s['text'][:70]}")
        if args.entropy_check:
            print(f"\nEntropy check: {entropy_flagged}/{len(selected)} flagged as high-difficulty")
        return

    # Create tasks. Each task is assigned round-robin across the fleet and
    # staged as 'ready'; the gateway spawns one per worker at a time and the
    # rest wait as the deep 'ready' overflow queue.
    used_workers = set()
    created = 0
    backlog = 0  # retained for output compatibility; always 0 now
    entropy_flagged = 0
    # Source-experiment fan-out cap: limit tasks per source experiment per batch
    # Prevents feedback loops where one experiment's curiosities dominate the batch
    SOURCE_CAP = 2  # max tasks per source experiment per batch
    source_batch_counts = {}
    _source_cap_re = re.compile(r"exp_(\d+\w*)")
    for i, s in enumerate(selected):
        # Reset the per-batch balance set each full sweep of the fleet, so a
        # deep queue stacks evenly (1 each, then 2 each, ...).
        if len(used_workers) >= len(WORKERS):
            used_workers = set()
        worker = pick_worker(free_workers, used_workers)
        if worker is None:
            print(f"  No workers configured; stopped after {created} created.")
            break

        # Source-experiment fan-out cap: skip if this source already has enough tasks this batch
        _src_m = _source_cap_re.search(s.get("text", ""))
        _src_id = f"exp_{_src_m.group(1)}" if _src_m else None
        if _src_id:
            if source_batch_counts.get(_src_id, 0) >= SOURCE_CAP:
                continue  # source experiment already at cap — skip to next item

        # Parse JSON-encoded text from queue items (curiosity_scorer may return {"text": "..."})
        raw_text = s['text']
        if raw_text.startswith('{'):
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict) and 'text' in parsed:
                    raw_text = parsed['text']
            except (json.JSONDecodeError, ValueError):
                pass
        # Normalize double brackets — workers sometimes emit [[TRANSFER...] instead of [TRANSFER...]
        raw_text = re.sub(r'\[\[', '[', raw_text)
        # Strip [TRANSFER] questions and brackets from title — title = hypothesis only
        # Keep [TRANSFER] prefix so safe_kanban_create can skip dedup for cross-domain items
        title_text = re.sub(r'\[TRANSFER.*?\]\s*[^;]*;?\s*', '', raw_text).strip()
        title_text = re.sub(r'\[.*?\]\s*', '', title_text).strip()
        title_text = re.sub(r'^;\s*', '', title_text).strip()
        title_text = re.sub(r';\s*$', '', title_text).strip()
        if not title_text:
            # Fallback: find first non-TRANSFER segment
            # Normalize double brackets in s['text'] for fallback too
            fallback_text = re.sub(r'\[\[', '[', s['text'])
            parts = fallback_text.split(';')
            for part in parts:
                clean_part = re.sub(r'\[.*?\]\s*', '', part).strip()
                if clean_part and len(clean_part) > 10:
                    title_text = clean_part
                    break
            if not title_text:
                title_text = fallback_text[:80]
        if len(title_text) > 200:
            title_text = title_text[:197] + "..."
        # Re-add [TRANSFER] prefix if original item had it — so safe_kanban_create
        # can identify cross-domain items and skip dedup
        if "[TRANSFER" in s['text']:
            title = f"exp_{next_id[0]}: [TRANSFER] {title_text}"
        else:
            title = f"exp_{next_id[0]}: {title_text}"
        next_id[0] += 1
        body = generate_task_body(s["text"], item_dict=s)

        # Pre-deployment entropy check (exp_72526101)
        entropy_tag = ""
        if args.entropy_check:
            diff, flagged, _ = entropy_preflight_check(s["text"])
            if flagged:
                entropy_flagged += 1
                entropy_tag = f" [ENTROPY={diff:.3f} FLAGGED]"
                body += f"\n\n⚠ PRE-DEPLOYMENT ENTROPY WARNING: difficulty_score={diff:.3f} (threshold={args.entropy_threshold}). This task has high signal concentration entropy, suggesting it may be harder to fine-tune on."
            else:
                entropy_tag = f" [entropy={diff:.3f}]"

        _priority = get_task_priority(s, "transfer" if "[TRANSFER" in s.get("text", "").upper() else "genuine")
        task_id = create_task(title, worker, body, priority=_priority)
        if task_id:
            used_workers.add(worker)
            created += 1
            if _src_id:
                source_batch_counts[_src_id] = source_batch_counts.get(_src_id, 0) + 1
            s["assignee"] = worker  # Track for JSON output
            print(f"  Created: {task_id} → {worker} [ready, staged] [{s['thread']}] score={s['total']}{entropy_tag}")

            # Log routing decision for topology visualization
            if ROUTING_LOG_AVAILABLE:
                source = s.get('source_domain') or s['thread']
                target = s['thread']
                if "[TRANSFER" in s['text']:
                    # For transfers: source is where it came from, target is where it's going
                    source = s.get('source_domain', s['thread'])
                    target = s['thread']
                log_routing_decision(source, target, [], s['text'][:500])

            # Transfer tracking: link task to tracking row
            if TRANSFER_TRACKING_AVAILABLE and "[TRANSFER" in s.get('text', ''):
                source_result_id = s.get('source_result_id')
                target_domain = parse_transfer_target(s['text']) or s.get('thread', 'unknown')
                if source_result_id and target_domain:
                    update_task_created(source_result_id, target_domain, task_id)
        else:
            print(f"  FAILED: {s['text'][:50]}")

    if args.entropy_check:
        print(f"\nEntropy check: {entropy_flagged}/{created} tasks flagged as high-difficulty")

    total_made = created + backlog
    print(f"\nCreated {total_made}/{len(selected)} tasks ({created} assigned, {backlog} ready-backlog)")

    # Record queue flow (items consumed)
    if total_made > 0:
        try:
            sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
            import health_signal
            health_signal.record_queue_consume(total_made)
        except Exception:
            pass


if __name__ == "__main__":
    main()
