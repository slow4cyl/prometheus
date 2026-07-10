#!/usr/bin/env python3
"""
write_worker_result.py — Write a structured experiment result to the worker_results table.

Usage:
    python3 ~/.hermes/scripts/write_worker_result.py \
      --experiment exp_NNN \
      --finding "CONFIRMED: F1=0.95 BECAUSE adversarial inputs cluster in low-dim subspace" \
      --supported \
      --confidence 0.85 \
      --domain injection_detection \
      --tags CONFIRMED,surprise \
      --files "exp_NNN.py,exp_NNN_results.json" \
      --queue "Follow-up question 1?;[TRANSFER] Cross-domain question?"
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import re

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from prometheus_db import get_db

"""CLI tool: Write Worker Result.

Usage: python3 write_worker_result.py [options]
"""


def get_db_path():
    """Return the canonical prometheus.db (single source of truth).

    Previously this preferred ghost copies under kanban/ and experiments/,
    which split worker results away from the brain DB and caused silent
    data loss. Now always returns the canonical ~/.hermes/prometheus.db.
    """
    # DELIBERATE hardcode — do NOT migrate to prometheus_paths: that module
    # honors HERMES_HOME, and honoring it here previously split worker
    # results away from the brain DB (silent data loss; see docstring).
    home = os.path.expanduser("~")
    canonical = os.path.join(home, ".hermes", "prometheus.db")
    if os.path.exists(canonical):
        return canonical
    raise FileNotFoundError(f"canonical prometheus.db not found at {canonical}")

def normalize_domain(domain):
    """Normalize a domain string to canonical form."""
    if not domain:
        return domain
    d = domain.strip().lower()
    d = re.sub(r'[\s\-/]+', '_', d)
    d = re.sub(r'[^a-z0-9_]', '', d)
    d = re.sub(r'_+', '_', d).strip('_')
    if not d:
        return domain
    merges = {
        # Spelling/format variants -> canonical (kept: these are true synonyms).
        'prompt_injection_detection': 'injection_detection',
        'prompt_injection': 'injection_detection',
        'security_injection_detection': 'injection_detection',
        'security_injection': 'injection_detection',
        'injection': 'injection_detection',
        'hallucination_detection': 'injection_detection',
        'cross_pollination': 'cross_domain',
        'meta_research': 'meta_analysis', 'meta_learning': 'meta_analysis',
        'embeddings': 'embedding',
        'financial_fraud_detection': 'finance', 'financial_markets': 'finance',
        'ai_safety': 'safety', 'ml_safety': 'safety',
        # NOTE: The following GREEDY/LOSSY mappings were REMOVED (June 8 2026)
        # because they were the ROOT CAUSE of domain mislabeling — they force
        # unrelated experiments into a few mega-buckets, fighting the embedding
        # classifier (which is now the source of truth, see
        # embedding_domain_classifier.DOMAIN_DESCRIPTIONS). Do NOT re-add:
        #   'general': 'calibration'   <- swept every unclassified exp into calibration
        #   'rlhf': 'calibration'      <- RLHF is not calibration
        #   'rag'/'rag_*': 'injection_detection'
        #   'defense': 'safety', 'attack': 'adversarial_ml'
        #   'ml_security': 'safety', 'tardigrade_biology': 'biology'
        #   'ensemble': 'ensemble_methods'  (embedding vocab uses 'ensemble')
        #   'adversarial_detection': 'adversarial_ml'
        #   'synthesis'/'meta'/'meta_cognition': 'meta_analysis'
        #   'cross_lingual'/'nlp_*': 'nlp', 'distillation': 'optimization'
        #   'dispatch': 'dispatch_pipeline', 'dict': 'dict_methodology'
        # If a domain is genuinely unknown, leave it AS-IS (or empty) so the
        # embedding classifier can assign it correctly downstream.
    }
    return merges.get(d, d)


def auto_classify_domain(experiment_id, finding):
    """Auto-classify domain using embedding classifier if available, fallback to regex."""
    try:
        from embedding_domain_classifier import classify_embedding
        title = finding[:200] if finding else experiment_id
        result, confidence = classify_embedding(title, "")
        if result and result != "general" and confidence > 0.3:
            return result
    except Exception:
        pass
    try:
        from domain_classifier import classify
        title = finding[:200] if finding else experiment_id
        return classify(title, finding)
    except Exception:
        pass
    return "general"


def clamp_confidence(val):
    """Clamp confidence to valid [0, 1] range. Warns on out-of-range values.
    
    Detects values that look like percents (2-100 range) and normalizes.
    Values > 1.0 that don't look like % scores are clamped with warning.
    """
    if val is None:
        return 0.85
    try:
        v = float(val)
    except (ValueError, TypeError):
        return 0.85
    if v < 0:
        return 0.0
    if 1.0 < v <= 100.0:
        # Likely a percentage or unnormalized score — divide by 100
        v = v / 100.0
        return round(v, 4)
    if v > 1.0:
        # Genuinely out of range (shouldn't happen after the above)
        return min(v, 1.0)
    return v

def verify_artifacts(files_list, experiment_id, created_at=None):
    """Check that every claimed artifact actually exists and isn't obviously broken.
    
    Returns: 'VERIFIED' if all pass, 'FAILED' if any fail, 'UNVERIFIED' if no files.
    
    Checks per file:
      - exists
      - non-empty (>0 bytes)
      - recent enough (within 24h of experiment creation, if created_at known)
      - parseable (json.load for .json, valid UTF-8 for text)
    """
    if not files_list:
        return 'UNVERIFIED'
    
    failures = []
    cwd = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))
    now = time.time()
    exp_time = created_at if created_at else now
    
    for fname in files_list:
        fname = fname.strip()
        if not fname:
            continue
        
        # Try relative to cwd first, then absolute
        fpath = fname if os.path.isabs(fname) else os.path.join(cwd, fname)
        if not os.path.exists(fpath):
            # Also try relative to workspace
            workspace = os.environ.get('HERMES_KANBAN_WORKSPACE')
            if workspace:
                ws_path = os.path.join(workspace, fname)
                if os.path.exists(ws_path):
                    fpath = ws_path
            if not os.path.exists(fpath):
                failures.append(f"{fname}: does not exist")
                continue
        
        # Non-empty
        try:
            size = os.path.getsize(fpath)
            if size == 0:
                failures.append(f"{fname}: empty file")
                continue
        except OSError as e:
            failures.append(f"{fname}: stat failed ({e})")
            continue
        
        # Recent enough: allow 24h window before experiment creation
        # (workers may write results after creating files)
        if created_at:
            try:
                mtime = os.path.getmtime(fpath)
                if mtime < exp_time - 86400:  # more than 24h before experiment
                    failures.append(f"{fname}: mtime ({mtime}) is before experiment start ({exp_time})")
                    continue
            except OSError:
                pass
        
        # Parseable — file-type aware
        ext = os.path.splitext(fname)[1].lower()
        try:
            if ext == '.json':
                with open(fpath, 'r') as f:
                    json.load(f)
            elif ext in ('.py', '.md', '.txt', '.csv', '.yaml', '.yml', '.toml', '.cfg', '.conf'):
                with open(fpath, 'r') as f:
                    f.read(65536)  # just check it reads as text
            elif ext == '.csv':
                with open(fpath, 'r') as f:
                    header = f.readline()
                    if not header.strip():
                        failures.append(f"{fname}: CSV has no header row")
                        continue
            # Other extensions (.pkl, .pt, .bin, images) — skip structural check
        except (json.JSONDecodeError, UnicodeDecodeError, IOError, OSError) as e:
            failures.append(f"{fname}: unparseable ({e})")
            continue
    
    if failures:
        detail = "; ".join(failures)
        print(f"ARTIFACT VERIFICATION FAILED for {experiment_id}: {detail}")
        return 'FAILED'
    
    print(f"ARTIFACT VERIFICATION PASSED for {experiment_id} ({len(files_list)} files)")
    return 'VERIFIED'


def calibrate_confidence(raw_confidence):
    """Map raw confidence to empirical accuracy (post-recalibration, June 2026).

    Raw scores measured familiarity, not accuracy. This mapping was derived
    from a full audit of 15,583 non-redundant findings — each bin's empirical
    contradiction rate defines its calibrated value.
    """
    CALIBRATION_MAP = {
        -0.30: 0.5455, 0.00: 0.5455, 0.10: 0.5455, 0.15: 0.5455,
        0.25: 0.5455, 0.30: 0.5455,  # all map to 54.5%
        0.35: 0.8462, 0.40: 0.7222, 0.45: 0.9615, 0.50: 0.7857,
        0.55: 0.8750, 0.60: 0.8318, 0.65: 0.8493, 0.70: 0.8630,
        0.75: 0.8152, 0.80: 0.8259, 0.85: 0.8260, 0.90: 0.8279,
        0.95: 0.8672, 1.00: 0.9167,
    }
    rounded = round(raw_confidence * 20) / 20
    if rounded in CALIBRATION_MAP:
        return round(CALIBRATION_MAP[rounded], 4)
    sorted_bins = sorted(CALIBRATION_MAP.keys())
    if rounded < sorted_bins[0]:
        return CALIBRATION_MAP[sorted_bins[0]]
    if rounded > sorted_bins[-1]:
        return CALIBRATION_MAP[sorted_bins[-1]]
    lower = max(b for b in sorted_bins if b <= rounded)
    upper = min(b for b in sorted_bins if b >= rounded)
    if lower == upper:
        return CALIBRATION_MAP[lower]
    t = (rounded - lower) / (upper - lower)
    return round(CALIBRATION_MAP[lower] * (1 - t) + CALIBRATION_MAP[upper] * t, 4)


def _model_from_task_override():
    """Provenance precedence (1): tasks.model_override for HERMES_KANBAN_TASK.

    Short read-only lookup against kanban.db (env HERMES_KANBAN_DB — the
    dispatcher exports it to every worker — else <HERMES_HOME>/kanban.db).
    Best-effort: any failure (missing env, missing DB, locked DB, schema
    drift) returns None so the caller falls through to the next source.
    """
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return None
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    db_path = (os.environ.get("HERMES_KANBAN_DB") or "").strip() or os.path.join(hermes_home, "kanban.db")
    if not os.path.exists(db_path):
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        conn.execute("PRAGMA busy_timeout=1500")
        row = conn.execute(
            "SELECT model_override FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row and row[0] and str(row[0]).strip():
            return str(row[0]).strip()
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def _model_from_env():
    """Provenance precedence (2): the pre-surgery env pins (harmless dual path)."""
    return os.environ.get('HERMES_MODEL') or os.environ.get('AUXILIARY_VISION_MODEL')


def _model_from_config():
    """Provenance precedence (3): the profile config's model.default.

    Cheap guarded read of <HERMES_HOME>/config.yaml — handles both the
    ``model: <string>`` and ``model: {default: <string>}`` shapes, the same
    pair the dispatcher's effective-model resolution accepts. Returns None
    on any failure (no yaml, no file, malformed config).
    """
    try:
        hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
        cfg_path = os.path.join(hermes_home, "config.yaml")
        if not os.path.exists(cfg_path):
            return None
        import yaml
        with open(cfg_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        if not isinstance(cfg, dict):
            return None
        m = cfg.get("model", "")
        if isinstance(m, dict):
            m = m.get("default") or ""
        m = str(m or "").strip()
        return m or None
    except Exception:
        return None


def resolve_model_provenance():
    """Resolve worker_results.model at WRITE time (defork 2026-07-08).

    Mirrors the dispatcher _default_spawn's effective-model precedence so
    provenance no longer depends on the fork's HERMES_MODEL spawn-time pin:
      (1) tasks.model_override via HERMES_KANBAN_TASK (read-only kanban.db)
      (2) HERMES_MODEL / AUXILIARY_VISION_MODEL env (pre-surgery dual path)
      (3) profile config model.default
      (4) None — the stamper sidecar backfills NULL rows.
    Every stage is exception-guarded: this sits on the fleet's result-write
    path and must never break a write.
    """
    for resolver in (_model_from_task_override, _model_from_env, _model_from_config):
        try:
            resolved = resolver()
        except Exception:
            resolved = None
        if resolved:
            return resolved
    return None


def write_result(experiment_id, finding, supported=None, confidence=0.85,
                 domain=None, tags=None, files=None, queue=None,
                 predicted_direction=None, observed_direction=None, design_vector=None,
                 kanban_task_id=None, worker_id=None, experiment_type=None,
                 mechanism_type=None, model=None):
    """Write a worker result to the database.
    
    experiment_type: MECHANISTIC | EMPIRICAL | EMPIRICAL_VALIDATED | ANALOGICAL
      - MECHANISTIC: testing if a known mechanism is sufficient (synthetic data OK)
      - EMPIRICAL: testing a prediction against real-world data (data may be synthetic)
      - EMPIRICAL_VALIDATED: confirmed against actual physical measurements
      - ANALOGICAL: testing whether a mechanism from domain A operates in domain B
    
    mechanism_type: UNIVERSAL_LAW | EMPIRICAL_CORRELATION (for ANALOGICAL only)
      - UNIVERSAL_LAW: first-principles math (thermodynamics, info theory, wave physics)
      - EMPIRICAL_CORRELATION: fitted relationships (biology scaling,材料 properties)
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=10000")
    
    tags_str = ",".join(tags) if tags else ""
    files_str = ",".join(files) if files else ""
    queue_str = ";".join(queue) if queue else ""
    
    # Auto-classify domain if not provided
    if not domain:
        domain = auto_classify_domain(experiment_id, finding)

    # Normalize domain to canonical form
    domain = normalize_domain(domain)

    # Resolve model provenance when the caller didn't pass one. The CLI path
    # (__main__) already does `args.model or resolve_model_provenance()`, but
    # in-process callers that invoke this function with the default model=None
    # bypassed it — so every default-model worker wrote model=NULL (~10% of
    # results/hour, measured 2026-07-10). Mirror the dispatcher's precedence
    # here (task override -> env -> profile config) so BOTH entry paths stamp.
    if not model:
        try:
            model = resolve_model_provenance()
        except Exception:
            model = None

    predicted_str = json.dumps(predicted_direction) if predicted_direction else None
    observed_str = json.dumps(observed_direction) if observed_direction else None
    design_str = json.dumps(design_vector) if design_vector else None
    
    # Validate experiment_type
    valid_types = ('MECHANISTIC', 'EMPIRICAL', 'EMPIRICAL_VALIDATED', 'ANALOGICAL')
    if experiment_type and experiment_type.upper() not in valid_types:
        print(f"WARNING: Invalid experiment_type '{experiment_type}', must be one of {valid_types}")
        experiment_type = None
    
    # Validate mechanism_type
    valid_mechanisms = ('UNIVERSAL_LAW', 'EMPIRICAL_CORRELATION')
    if mechanism_type and mechanism_type.upper() not in valid_mechanisms:
        print(f"WARNING: Invalid mechanism_type '{mechanism_type}', must be one of {valid_mechanisms}")
        mechanism_type = None
    
    # Validate and clamp confidence to [0, 1]
    safe_confidence = clamp_confidence(confidence)
    if safe_confidence != confidence:  # noqa: compare raw vs clamped
        import warnings
        warnings.warn(f"Confidence {confidence} outside [0,1], clamped to {safe_confidence}")
    
    # Run artifact verification before writing
    artifact_status = verify_artifacts(files or [], experiment_id, created_at=int(time.time()))
    calibrated = calibrate_confidence(safe_confidence)
    
    # Confidence transparency: always log both values
    print(f"CONFIDENCE: raw={safe_confidence:.4f} | calibrated={calibrated:.4f} | "
          f"gap={abs(calibrated - safe_confidence):.4f} | artifact={artifact_status}")

    cur = conn.execute("""
        INSERT OR REPLACE INTO worker_results
        (experiment_id, kanban_task_id, hypothesis_supported, key_finding,
         confidence, calibrated_confidence, domain, tags, files_produced, queue_additions,
         worker_id, created_at, applied,
         predicted_direction, observed_direction, design_vector,
         experiment_type, mechanism_type, bridge_attempts, model, artifact_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?)
    """, (
        experiment_id,
        kanban_task_id,
        1 if supported else (0 if supported is not None else None),
        finding,
        safe_confidence,
        calibrated,
        domain,
        tags_str,
        files_str,
        queue_str,
        worker_id,
        int(time.time()),
        predicted_str,
        observed_str,
        design_str,
        experiment_type.upper() if experiment_type else None,
        mechanism_type.upper() if mechanism_type else None,
        model,
        artifact_status,
    ))
    conn.commit()

    # ROOT-CAUSE FIX (2026-06-08): populate transfer_tracking at WRITE time, not
    # only during the apply/queue step (which silently skipped already-applied rows,
    # leaving transfer_tracking at 0 rows). Any [TRANSFER] item in the queue records
    # a queued cross-domain transfer keyed on this worker_result. Best-effort: never
    # break the result-write path.
    try:
        _src_id = cur.lastrowid
        if _src_id and queue_str and "TRANSFER" in queue_str.upper() and domain:
            import re as _re
            _src_dom = _re.sub(r'[\s\-/]+', '_', str(domain).strip().lower())
            for _item in (queue or []):
                if "TRANSFER" not in _item.upper():
                    continue
                _m = _re.search(r"\[TRANSFER\s+from\s+([a-zA-Z0-9_\- ]+)\]", _item, _re.I)
                _tgt = _re.sub(r'[\s\-/]+', '_', _m.group(1).strip().lower()) if _m else _src_dom
                conn.execute(
                    "INSERT OR IGNORE INTO transfer_tracking "
                    "(source_result_id, source_domain, target_domain, status) "
                    "VALUES (?, ?, ?, 'queued')",
                    (_src_id, _src_dom, _tgt))
            conn.commit()
    except Exception as _e:
        print(f"Warning: transfer_tracking write skipped: {_e}")

    conn.close()
    print(f"Result written for {experiment_id} (type={experiment_type}, mechanism={mechanism_type}, db: {db_path})")
    
    # Also write to ~/.hermes/prometheus.db if it exists and is different
    home = os.path.expanduser("~")
    enforcement_db = os.path.join(home, ".hermes", "prometheus.db")
    if os.path.exists(enforcement_db) and os.path.abspath(enforcement_db) != os.path.abspath(db_path):
        try:
            econn = sqlite3.connect(enforcement_db, timeout=30)
            econn.execute("PRAGMA busy_timeout=30000")
            econn.execute("PRAGMA synchronous=NORMAL")
            econn.execute("""INSERT INTO worker_results
                (experiment_id, kanban_task_id, hypothesis_supported, key_finding,
                 confidence, calibrated_confidence, domain, tags, files_produced, queue_additions,
                 worker_id, created_at, applied, predicted_direction, observed_direction,
                 design_vector, experiment_type, mechanism_type, model, artifact_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
                (experiment_id, kanban_task_id,
                 1 if supported else (0 if supported is not None else None),
                 finding, safe_confidence, calibrated, domain, tags_str, files_str, queue_str,
                 worker_id, int(time.time()), predicted_str, observed_str, design_str,
                 experiment_type.upper() if experiment_type else None,
                 mechanism_type.upper() if mechanism_type else None,
                 model, artifact_status))
            econn.commit()
            econn.close()
        except Exception as e:
            print(f"Warning: could not write to enforcement db: {e}")

def main():
    parser = argparse.ArgumentParser(description="Write worker experiment result")
    parser.add_argument("--experiment", required=True, help="Experiment ID (e.g. exp_NNN)")
    parser.add_argument("--finding", required=True, help="Key finding with mechanism")
    parser.add_argument("--supported", action="store_true", help="Hypothesis supported")
    parser.add_argument("--refuted", action="store_true", help="Hypothesis refuted")
    parser.add_argument("--confidence", type=float, default=0.85, help="Confidence (0-1)")
    parser.add_argument("--domain", default=None, help="Domain (auto-classified if omitted)")
    parser.add_argument("--tags", default="", help="Comma-separated tags")
    parser.add_argument("--files", default="", help="Comma-separated file paths")
    parser.add_argument("--queue", default="", help="Semicolon-separated queue items")
    parser.add_argument("--kanban-task", default=None, help="Kanban task ID")
    parser.add_argument("--worker", default=None, help="Worker ID")
    parser.add_argument("--type", default=None, choices=['MECHANISTIC', 'EMPIRICAL', 'EMPIRICAL_VALIDATED', 'ANALOGICAL'],
                        help="Experiment type: MECHANISTIC (synthetic OK), EMPIRICAL (real data desired), EMPIRICAL_VALIDATED (confirmed against real measurements), ANALOGICAL (cross-domain)")
    parser.add_argument("--mechanism-type", default=None, choices=['UNIVERSAL_LAW', 'EMPIRICAL_CORRELATION'],
                        help="Mechanism type (ANALOGICAL only): UNIVERSAL_LAW (first-principles math) or EMPIRICAL_CORRELATION (fitted relationships)")
    parser.add_argument("--predicted-direction", default=None, help="JSON dict")
    parser.add_argument("--observed-direction", default=None, help="JSON dict")
    parser.add_argument("--design-vector", default=None, help="JSON dict")
    parser.add_argument("--model", default=None, help="Model that ran this experiment (for provenance tracking)")
    
    args = parser.parse_args()
    
    # Auto-detect model if not provided: write-time provenance resolution
    # (tasks.model_override > env pins > config model.default > NULL).
    model = args.model or resolve_model_provenance()
    
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    files = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else []
    queue = [q.strip() for q in args.queue.split(";") if q.strip()] if args.queue else []
    
    supported = None
    if args.supported:
        supported = True
    elif args.refuted:
        supported = False
    
    predicted = json.loads(args.predicted_direction) if args.predicted_direction else None
    observed = json.loads(args.observed_direction) if args.observed_direction else None
    design = json.loads(args.design_vector) if args.design_vector else None
    
    safe_confidence = clamp_confidence(args.confidence)
    
    write_result(
        experiment_id=args.experiment,
        finding=args.finding,
        supported=supported,
        confidence=safe_confidence,
        domain=args.domain,
        tags=tags,
        files=files,
        queue=queue,
        kanban_task_id=args.kanban_task or os.environ.get("HERMES_KANBAN_TASK"),
        worker_id=args.worker,
        experiment_type=args.type,
        mechanism_type=args.mechanism_type,
        predicted_direction=predicted,
        observed_direction=observed,
        design_vector=design,
        model=model,
    )

if __name__ == "__main__":
    main()
