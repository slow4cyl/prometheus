#!/usr/bin/env python3
"""
claim_lifecycle.py — Knowledge claim lifecycle management.

Bridges the gap between experiment-centric storage and claim-centric belief tracking.
Groups experiments by hypothesis, attaches evidence, computes posteriors,
and manages claim status transitions.

This is the missing transformation layer:
  evidence → aggregation → confidence → status

Usage:
    python3 claim_lifecycle.py init                    # Create tables
    python3 claim_lifecycle.py populate                # Group existing experiments into claims
    python3 claim_lifecycle.py attach <exp_id>         # Attach a specific experiment to its claim
    python3 claim_lifecycle.py attach-all              # Attach all unattached experiments
    python3 claim_lifecycle.py posterior <claim_id>    # Recompute posterior for a claim
    python3 claim_lifecycle.py posterior-all           # Recompute all posteriors
    python3 claim_lifecycle.py list                    # List all knowledge claims
    python3 claim_lifecycle.py list --status CONTESTED # Filter by status
    python3 claim_lifecycle.py status <claim_id>       # Show claim details + evidence
    python3 claim_lifecycle.py retired                 # Show retired claims
    python3 claim_lifecycle.py summary                 # Aggregate statistics
"""

import argparse
from prometheus_paths import PROMETHEUS_DB as _PP_PROMETHEUS_DB
import json
import os
import re
import sqlite3
import sys
import hashlib
from collections import Counter
from datetime import datetime

DB_PATH = _PP_PROMETHEUS_DB
ACTIVE = "ACTIVE"           # support > 80%, 3+ tests
CONDITIONAL = "CONDITIONAL" # support 50-80%
CONTESTED = "CONTESTED"     # support < 50%
RETIRED = "RETIRED"         # support < 20% with 10+ tests, or explicitly retired
UNTESTED = "UNTESTED"       # 0-2 tests, no clear signal

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_TESTS_FOR_ACTIVE = 3
MIN_TESTS_FOR_RETIRED = 10
ACTIVE_THRESHOLD = 0.80
CONDITIONAL_THRESHOLD = 0.50
CONTESTED_THRESHOLD = 0.20


def get_db():
    """Get database connection with retry protection."""
    try:
        from db_retry import get_db as _get_db
        return _get_db(DB_PATH)
    except ImportError:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn


def normalize_hypothesis(text):
    """Normalize hypothesis text for grouping / claim identity (hypothesis_hash).

    A claim's identity is the SCIENTIFIC QUESTION, not the orchestration framing
    the task creators wrap it in. In this system a leading `[...]` is ALWAYS
    framing — `[TRANSFER from X]`, `[CANDIDATE-RETEST]`, `[COMPRESSION-BOUNDARY]`,
    `[BOUNDARY]`, `[NOVEL]`, `[SYSTEM-LEVEL]`, `[NEW from synthesis vNNN]`,
    `[curiosity #N from exp_M]`, `[sparse: domain]`, etc. — never part of the
    hypothesis. Strips:
      - all leading orchestration tag(s) (any bracketed prefix)
      - retest boilerplate ("Replicate and validate: …")
      - trailing routing/repro stats ("(SR=83%, 6 transfers, routing=0.661)")
      - experiment IDs (exp_NNNN)

    INVARIANT (verified 2026-07-04): produces byte-identical output to the prior
    version on UNTAGGED text, so existing clean claims keep their claim_hash —
    only tagged experiments re-group (retests now attach to the claim they
    retest instead of forking a parallel one). Do NOT lowercase or strip
    numbers here — that WOULD change existing hashes and orphan the store.

    Returns a normalized string for hashing/grouping.
    """
    if not text:
        return ""

    t = text.strip()

    # Strip leading orchestration tag(s) — bounded to avoid eating a genuine
    # bracketed clause; repeated for stacked tags like "[TRANSFER][NOVEL]".
    for _ in range(3):
        nt = re.sub(r'^\s*\[[^\]]{1,60}\]\s*', '', t)
        if nt == t:
            break
        t = nt

    # Retest/validate boilerplate that precedes the real hypothesis
    t = re.sub(r'^(replicate and validate|validate and replicate|replicate|validate|retest)\s*:?\s+',
               '', t, flags=re.I)

    # Trailing routing/repro stats appended by the enqueuers
    t = re.sub(r'\s*\((?:SR=|routing=|[\d.]+%\s*success)[^)]*\)\s*$', '', t, flags=re.I)

    # Remove experiment ID references (unchanged from prior version)
    t = re.sub(r'exp_\d+', '', t)

    # Normalize whitespace (unchanged from prior version)
    t = re.sub(r'\s+', ' ', t).strip()

    return t


def hypothesis_hash(text):
    """Compute a stable hash for a normalized hypothesis."""
    normalized = normalize_hypothesis(text)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def classify_claim_directionality(hypothesis_text):
    """Classify a hypothesis as DIRECTIONAL or NON_DIRECTIONAL.
    
    DIRECTIONAL: The hypothesis predicts a specific outcome — "X causes Y",
    "X is better than Y", "X predicts Y", "X transfers to Y". The binary
    support/refute flag is meaningful because the hypothesis commits to a
    direction.
    
    NON_DIRECTIONAL: The hypothesis is an open question — "How does X affect
    Y?", "What is the minimum X for Y?", "Does X affect Y?". Every worker
    who finds *some* answer marks hypothesis_supported=1, regardless of
    whether their findings agree. The posterior is inflated because the
    binary flag cannot distinguish "I found the same thing" from "I found
    something different."
    
    Returns: 'DIRECTIONAL' or 'NON_DIRECTIONAL'
    """
    if not hypothesis_text:
        return 'DIRECTIONAL'
    
    t = hypothesis_text.strip()
    t_lower = t.lower()
    
    # Strip leading markers like [TRANSFER], [NEW], [NOVEL], numbers
    t_clean = re.sub(r'^\[.*?\]\s*', '', t)
    t_clean = re.sub(r'^\d+\.\s*', '', t_clean)
    t_clean_lower = t_clean.lower()
    
    # NON_DIRECTIONAL patterns — open questions with no predicted direction
    non_directional_patterns = [
        # "How does/what/which..." questions
        (r'^how\s+(does|do|can|will|should|could|would|is|are|to)', t_clean_lower),
        (r'^what\s+(is|are|was|were|does|do|can|will|should|could|would|the|minimum|maximum)', t_clean_lower),
        (r'^which\s+(is|are|does|do|can|factors|method|approach)', t_clean_lower),
        (r'^where\s+(is|are|does|do)', t_clean_lower),
        (r'^when\s+(is|are|does|do)', t_clean_lower),
        # "Does X transfer to Y?" without a predicted direction
        # (Note: "Does X transfer" is borderline — it commits to transfer happening
        # or not, so we keep it as DIRECTIONAL unless it starts with "How" or "What")
        # Questions ending in "?" that don't start with "Does/Is/Can/Will" (which
        # commit to a direction)
        (r'^to what (extent|degree)', t_clean_lower),
        (r'^under what conditions', t_clean_lower),
    ]
    
    for pattern, text in non_directional_patterns:
        if re.match(pattern, text):
            return 'NON_DIRECTIONAL'
    
    # Check if it's a question (ends with ?) AND starts with a non-committal word
    if t_clean.endswith('?'):
        # "Does X cause Y?" — DIRECTIONAL (commits to yes/no)
        # "Is X better than Y?" — DIRECTIONAL
        # "Can X achieve Y?" — DIRECTIONAL
        # "How/What/Which..." — already caught above
        # "Does the threshold shift..." — DIRECTIONAL
        committal_starters = ['does', 'is', 'are', 'can', 'will', 'should', 'could', 'would', 'has', 'have', 'do']
        first_word = t_clean_lower.split()[0] if t_clean_lower.split() else ''
        if first_word not in committal_starters and not t_clean_lower.startswith(('how', 'what', 'which', 'where', 'when')):
            # Unusual question structure — check if it makes a prediction
            # If it contains "better", "improves", "reduces", etc. it's directional
            direction_words = ['better', 'improves', 'reduces', 'increases', 'decreases', 
                             'causes', 'predicts', 'transfers', 'applies', 'holds',
                             'fails', 'works', 'beats', 'outperforms', 'correlates']
            if not any(dw in t_clean_lower for dw in direction_words):
                return 'NON_DIRECTIONAL'
    
    return 'DIRECTIONAL'


def extract_support_from_result(worker_result_row):
    """Determine if a worker result supports or refutes its hypothesis.
    
    Returns: 1 (support), 0 (refute), None (ambiguous)
    
    Priority: hypothesis_supported column > supported column > key_finding text.
    BUT: if the finding text starts with a clear verdict word ("REFUTED:", 
    "CONFIRMED:") that CONTRADICTS the structured flag, prefer the text verdict.
    Workers routinely write one thing in free text and select another in the 
    binary field — the text verdict at the start of the finding is intentional.
    """
    flag_verdict = None

    # Structured flag (highest priority for initial determination)
    if worker_result_row['hypothesis_supported'] is not None:
        flag_verdict = 1 if worker_result_row['hypothesis_supported'] else 0
    elif worker_result_row['supported'] is not None:
        if isinstance(worker_result_row['supported'], str):
            flag_verdict = 1 if worker_result_row['supported'] == 'SUPPORTED' else 0
        else:
            flag_verdict = 1 if worker_result_row['supported'] else 0

    # Text verdict check — scan key_finding for direction words at the START
    finding = (worker_result_row['key_finding'] or '') + (worker_result_row['finding'] or '')
    finding_upper = finding.upper().strip()
    text_verdict = None
    if finding_upper.startswith('CONFIRMED REFUT') or finding_upper.startswith('CONFIRMED REFUTE'):
        text_verdict = 0  # "CONFIRMED REFUTATION" = confirming a refutation
    elif finding_upper.startswith('REFUTED:') or finding_upper.startswith('REFUTED '):
        if 'PARTIALLY' not in finding_upper[:30]:
            text_verdict = 0
    elif finding_upper.startswith('CONFIRMED:') or finding_upper.startswith('CONFIRMED '):
        text_verdict = 1
    elif finding_upper.startswith('SUPPORTED:') or finding_upper.startswith('SUPPORTED '):
        text_verdict = 1
    elif finding_upper.startswith('REFUTE ') or finding_upper.startswith('REFUTE:'):
        text_verdict = 0

    # Cross-check: if flag and text disagree, prefer the text verdict.
    # The worker wrote the verdict word intentionally at the start of their
    # finding. The binary flag may have been a misclick or default value.
    if flag_verdict is not None and text_verdict is not None and flag_verdict != text_verdict:
        print(f"⚠️  TEXT-FLAG MISMATCH in wr#{worker_result_row['id']}: "
              f"flag={flag_verdict} but finding starts with "
              f"'{finding[:40]}' → preferring text verdict={text_verdict}")
        return text_verdict

    if flag_verdict is not None:
        return flag_verdict

    # Fall back to broader text analysis (not just start-of-string)
    if 'REFUTED' in finding_upper and 'PARTIALLY' not in finding_upper:
        return 0
    if 'CONFIRMED' in finding_upper or 'SUPPORTED' in finding_upper:
        return 1
    if 'PARTIAL' in finding_upper:
        return None  # Ambiguous

    return None


def classify_refutation_type(refutation_type):
    """Map experiment refutation_type to evidence type."""
    mapping = {
        'SUPPORTED': 'support',
        'BOUNDARY': 'boundary',     # Partial — works here but not there
        'MECHANISTIC': 'mechanistic',  # Mechanism identified, may be domain-specific
        'STATISTICAL': 'statistical',  # Statistical test result
        'UNCERTAIN': 'uncertain',
        'REFUTED': 'refute',
    }
    return mapping.get(refutation_type, 'unknown')


def compute_posterior(support_count, refute_count, boundary_count=0):
    """Compute posterior belief from evidence counts.
    
    Uses a simple Bayesian approach with a weak prior (Beta(1,1) = uniform).
    This avoids extreme posteriors from small sample sizes.
    
    Returns: float 0.0-1.0
    """
    # Beta prior: alpha=1, beta=1 (uniform)
    alpha_prior = 1.0
    beta_prior = 1.0
    
    # Each support increases alpha, each refute increases beta
    # Boundary counts split the difference (0.5 weight each)
    alpha_posterior = alpha_prior + support_count + (boundary_count * 0.5)
    beta_posterior = beta_prior + refute_count + (boundary_count * 0.5)
    
    return alpha_posterior / (alpha_posterior + beta_posterior)


def determine_status(posterior, total_tests, support_count, refute_count):
    """Determine claim status from posterior and test count.
    
    Escalation rules (auto-retire when evidence is clearly against):
    1. support_ratio < 0.20 with 10+ tests → RETIRED (strong evidence against)
    2. 50+ tests with posterior < 0.35 → RETIRED (high volume, clearly false)
    3. 100+ tests with posterior < 0.40 → RETIRED (very high volume, likely false)
    4. 200+ tests with posterior < 0.45 → RETIRED (massive evidence, probably false)
    5. Standard thresholds for lower volumes
    
    Note: claims with 100+ tests and posterior ~0.50 stay CONTESTED — they are
    genuinely uncertain, not clearly false. Only retire when evidence actively
    points against.
    """
    if total_tests < MIN_TESTS_FOR_ACTIVE:
        return UNTESTED
    
    support_ratio = support_count / max(support_count + refute_count, 1)
    
    # Escalation Rule 1: Low support ratio with enough tests
    if total_tests >= MIN_TESTS_FOR_RETIRED and support_ratio < CONTESTED_THRESHOLD:
        return RETIRED
    
    # Escalation Rule 2: High volume, clearly false
    if total_tests >= 50 and posterior < 0.35:
        return RETIRED
    
    # Escalation Rule 3: Very high volume, likely false
    if total_tests >= 100 and posterior < 0.40:
        return RETIRED
    
    # Escalation Rule 4: Massive evidence, probably false
    if total_tests >= 200 and posterior < 0.45:
        return RETIRED
    
    # Standard thresholds
    if posterior >= ACTIVE_THRESHOLD:
        return ACTIVE
    elif posterior >= CONDITIONAL_THRESHOLD:
        return CONDITIONAL
    elif posterior >= CONTESTED_THRESHOLD:
        return CONTESTED
    else:
        return RETIRED


def check_consecutive_refutation(conn, claim_id, current_status):
    """Check if the most recent evidence entries are all refutations.
    
    If the last 5+ tests are all refutes, escalate to RETIRED regardless
    of posterior. This catches claims that are actively being disproven
    even if historical support keeps the posterior above threshold.
    
    Returns: new status (may be same as current_status).
    """
    if current_status == RETIRED:
        return RETIRED  # Already retired
    
    # Get the last 10 evidence entries ordered by attachment time
    recent = conn.execute("""
        SELECT evidence_type FROM claim_evidence
        WHERE claim_id = ?
        ORDER BY id DESC LIMIT 10
    """, (claim_id,)).fetchall()
    
    if len(recent) < 5:
        return current_status  # Not enough recent evidence
    
    # Count consecutive refutes from the end
    consecutive_refutes = 0
    for r in recent:
        if r[0] == 'refute':
            consecutive_refutes += 1
        else:
            break
    
    if consecutive_refutes >= 5:
        return RETIRED  # Active disproof streak
    
    return current_status


def init_schema(conn):
    """Create the knowledge_claims and claim_evidence tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_hash TEXT UNIQUE NOT NULL,        -- hash of normalized hypothesis
            hypothesis_text TEXT NOT NULL,           -- original hypothesis (first seen)
            normalized_text TEXT,                    -- normalized for grouping
            claim_summary TEXT,                      -- human-readable summary
            domain TEXT,                             -- primary domain
            posterior REAL DEFAULT 0.5,              -- Bayesian posterior belief
            support_count INTEGER DEFAULT 0,
            refute_count INTEGER DEFAULT 0,
            boundary_count INTEGER DEFAULT 0,
            uncertain_count INTEGER DEFAULT 0,
            total_evidence INTEGER DEFAULT 0,
            status TEXT DEFAULT 'UNTESTED',          -- ACTIVE/CONDITIONAL/CONTESTED/RETIRED/UNTESTED
            created_at TEXT DEFAULT (CAST(strftime('%s','now') AS REAL)),
            last_updated_at TEXT DEFAULT (datetime('now')),
            first_experiment_id TEXT,                -- first experiment testing this claim
            last_experiment_id TEXT,                 -- most recent experiment testing this claim
            claim_type TEXT DEFAULT 'DIRECTIONAL'    -- DIRECTIONAL (hypothesis with predicted direction) 
                                                      -- or NON_DIRECTIONAL (open question, binary
                                                      -- support/refute is meaningless)
        );
        
        CREATE INDEX IF NOT EXISTS idx_kc_status ON knowledge_claims(status);
        CREATE INDEX IF NOT EXISTS idx_kc_hash ON knowledge_claims(claim_hash);
        CREATE INDEX IF NOT EXISTS idx_kc_domain ON knowledge_claims(domain);
        CREATE INDEX IF NOT EXISTS idx_kc_posterior ON knowledge_claims(posterior);
        
        CREATE TABLE IF NOT EXISTS claim_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL REFERENCES knowledge_claims(id),
            experiment_id TEXT NOT NULL,
            worker_result_id INTEGER,
            evidence_type TEXT,                      -- support/refute/boundary/uncertain
            confidence REAL,
            key_finding TEXT,
            domain TEXT,
            attached_at TEXT DEFAULT (CAST(strftime('%s','now') AS REAL)),
            UNIQUE(claim_id, worker_result_id)          -- one evidence entry per worker_result per claim
        );
        
        CREATE INDEX IF NOT EXISTS idx_ce_claim ON claim_evidence(claim_id);
        CREATE INDEX IF NOT EXISTS idx_ce_experiment ON claim_evidence(experiment_id);
    """)
    
    # Add claim_type column if it doesn't exist (for existing databases)
    try:
        conn.execute("SELECT claim_type FROM knowledge_claims LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN claim_type TEXT DEFAULT 'DIRECTIONAL'")
        print("Added claim_type column to knowledge_claims")
    
    conn.commit()
    print("Schema created: knowledge_claims, claim_evidence")


def refresh_claim_summary(conn, claim_id, support=None, refute=None):
    """Fill knowledge_claims.claim_summary with the claim's best worker finding,
    so a claim displays its ANSWER, not the question it was created from
    (hypothesis_text is the prompt and stays the claim's identity/hash key).

    Uses the same selector as backfill_claim_summary.py. Non-destructive: only
    writes a non-empty summary, never clears an existing one, never touches
    hypothesis_text. Runs on the live intake path, so it is fully best-effort:
    ANY failure is swallowed and can never break claim attachment."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from backfill_claim_summary import pick_summary
        if support is None or refute is None:
            row = conn.execute(
                "SELECT support_count, refute_count FROM knowledge_claims WHERE id=?",
                (claim_id,)).fetchone()
            if not row:
                return
            support, refute = row[0] or 0, row[1] or 0
        rows = conn.execute(
            "SELECT evidence_type, confidence, key_finding, id FROM claim_evidence WHERE claim_id=?",
            (claim_id,)).fetchall()
        summ = pick_summary([(r[0], r[1], r[2], r[3]) for r in rows], support, refute)
        if summ:
            conn.execute("UPDATE knowledge_claims SET claim_summary=? WHERE id=?",
                         (summ, claim_id))
    except Exception:
        pass          # summary is a display nicety; never let it break claim intake


def populate_claims(conn):
    """Group existing experiments into claims based on hypothesis similarity."""
    
    # Get all experiments with hypotheses
    rows = conn.execute("""
        SELECT id, hypothesis, domain, refutation_type, created_at
        FROM experiments
        WHERE hypothesis IS NOT NULL AND length(hypothesis) > 10
        ORDER BY created_at ASC
    """).fetchall()
    
    print(f"Processing {len(rows)} experiments...")
    
    # Group by hypothesis hash
    claim_groups = {}
    for row in rows:
        h_hash = hypothesis_hash(row['hypothesis'])
        if h_hash not in claim_groups:
            claim_groups[h_hash] = {
                'hypothesis': row['hypothesis'],
                'normalized': normalize_hypothesis(row['hypothesis']),
                'domain': row['domain'],
                'experiments': [],
            }
        claim_groups[h_hash]['experiments'].append(row)
    
    print(f"Found {len(claim_groups)} unique claims")
    
    # Create claims and attach evidence
    created = 0
    for h_hash, group in claim_groups.items():
        # Check if claim already exists
        existing = conn.execute(
            "SELECT id FROM knowledge_claims WHERE claim_hash = ?", (h_hash,)
        ).fetchone()
        
        if existing:
            claim_id = existing['id']
        else:
            # Use the most common domain for the claim
            domains = [e['domain'] for e in group['experiments'] if e['domain']]
            primary_domain = Counter(domains).most_common(1)[0][0] if domains else None
            
            # is_meta (2026-07-01): tag claims about the system itself at
            # creation so leaderboards can exclude self-measurement.
            try:
                from meta_claim_classifier import is_meta_claim
                _is_meta = 1 if is_meta_claim(group['hypothesis'])[0] else 0
            except Exception:
                _is_meta = None
            # is_empirical_fact (2026-07-04): tag lookup-facts (named real-world
            # events verified, not mechanisms discovered) so the science
            # leaderboard excludes them the same way it excludes is_meta.
            try:
                from empirical_fact_classifier import is_empirical_fact
                _is_fact = 1 if is_empirical_fact(group['hypothesis'])[0] else 0
            except Exception:
                _is_fact = None
            conn.execute("""
                INSERT INTO knowledge_claims
                (claim_hash, hypothesis_text, normalized_text, domain,
                 first_experiment_id, last_experiment_id,
                 created_at, last_updated_at, is_meta, is_empirical_fact)
                VALUES (?, ?, ?, ?, ?, ?,
                        CAST(strftime('%s','now') AS REAL), CAST(strftime('%s','now') AS REAL), ?, ?)
            """, (
                h_hash,
                group['hypothesis'],
                group['normalized'],
                primary_domain,
                group['experiments'][0]['id'],
                group['experiments'][-1]['id'],
                _is_meta,
                _is_fact,
            ))
            claim_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            created += 1
        
        # Attach evidence for each experiment
        for exp in group['experiments']:
            # Get worker results for this experiment
            wrs = conn.execute("""
                SELECT id, hypothesis_supported, supported, key_finding, 
                       finding, domain, confidence
                FROM worker_results
                WHERE experiment_id = ?
            """, (exp['id'],)).fetchall()
            
            if not wrs:
                # No worker results yet — use refutation_type from experiments table
                evidence_type = classify_refutation_type(exp['refutation_type'])
                conn.execute("""
                    INSERT OR IGNORE INTO claim_evidence
                    (claim_id, experiment_id, evidence_type, domain)
                    VALUES (?, ?, ?, ?)
                """, (claim_id, exp['id'], evidence_type, exp['domain']))
            else:
                for wr in wrs:
                    support = extract_support_from_result(wr)
                    if support == 1:
                        evidence_type = 'support'
                    elif support == 0:
                        evidence_type = 'refute'
                    else:
                        # Check refutation_type for disambiguation
                        evidence_type = classify_refutation_type(exp['refutation_type'])

                    kf = wr['key_finding'] or wr['finding']

                    # INSERT OR IGNORE + UNIQUE INDEX ux_claim_evidence_dedup
                    # handles dedup atomically. No pre-check needed.
                    conn.execute("""
                        INSERT OR IGNORE INTO claim_evidence
                        (claim_id, experiment_id, worker_result_id, evidence_type,
                         confidence, key_finding, domain)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        claim_id, exp['id'], wr['id'], evidence_type,
                        wr['confidence'], kf,
                        wr['domain'] or exp['domain'],
                    ))
    
    conn.commit()
    print(f"Created {created} claims, attached evidence from {len(rows)} experiments")
    
    # Compute all posteriors
    compute_all_posteriors(conn)


def compute_all_posteriors(conn):
    """Recompute posterior and status for all claims."""
    claims = conn.execute("SELECT id FROM knowledge_claims").fetchall()
    
    updated = 0
    for claim in claims:
        claim_id = claim['id']
        
        # Count evidence by type
        counts = conn.execute("""
            SELECT evidence_type, COUNT(*) as cnt
            FROM claim_evidence
            WHERE claim_id = ?
            GROUP BY evidence_type
        """, (claim_id,)).fetchall()
        
        count_dict = {r['evidence_type']: r['cnt'] for r in counts}
        support = count_dict.get('support', 0)
        refute = count_dict.get('refute', 0)
        boundary = count_dict.get('boundary', 0)
        uncertain = count_dict.get('uncertain', 0)
        total = support + refute + boundary + uncertain
        
        posterior = compute_posterior(support, refute, boundary)
        status = determine_status(posterior, total, support, refute)
        
        # Escalation: check for consecutive refutation streak
        status = check_consecutive_refutation(conn, claim_id, status)
        
        conn.execute("""
            UPDATE knowledge_claims
            SET posterior = ?, support_count = ?, refute_count = ?,
                boundary_count = ?, uncertain_count = ?, total_evidence = ?,
                status = ?, last_updated_at = CAST(strftime('%s','now') AS REAL)
            WHERE id = ?
        """, (posterior, support, refute, boundary, uncertain, total, status, claim_id))
        refresh_claim_summary(conn, claim_id, support, refute)
        updated += 1

    conn.commit()
    print(f"Updated posteriors for {updated} claims")
    
    # Summary
    statuses = conn.execute("""
        SELECT status, COUNT(*) as cnt FROM knowledge_claims GROUP BY status
    """).fetchall()
    for s in statuses:
        print(f"  {s['status']}: {s['cnt']}")


def attach_experiment(conn, experiment_id):
    """Attach a single experiment to its claim and recompute posterior."""
    exp = conn.execute("""
        SELECT id, hypothesis, domain, refutation_type
        FROM experiments WHERE id = ?
    """, (experiment_id,)).fetchone()
    
    if not exp or not exp['hypothesis']:
        print(f"Experiment {experiment_id} not found or has no hypothesis")
        return
    
    h_hash = hypothesis_hash(exp['hypothesis'])
    
    # Get or create claim
    claim = conn.execute(
        "SELECT id FROM knowledge_claims WHERE claim_hash = ?", (h_hash,)
    ).fetchone()
    
    if not claim:
        # Classify directionality before creating the claim
        claim_type = classify_claim_directionality(exp['hypothesis'])
        if claim_type == 'NON_DIRECTIONAL':
            print(f"ℹ️  NON-DIRECTIONAL claim detected: \"{exp['hypothesis'][:80]}\" — "
                  f"binary support/refute may not be meaningful")
        # is_meta (2026-07-01): tag system-about-itself claims at creation.
        try:
            from meta_claim_classifier import is_meta_claim
            _is_meta = 1 if is_meta_claim(exp['hypothesis'])[0] else 0
        except Exception:
            _is_meta = None
        # is_empirical_fact (2026-07-04): tag lookup-facts at creation.
        try:
            from empirical_fact_classifier import is_empirical_fact
            _is_fact = 1 if is_empirical_fact(exp['hypothesis'])[0] else 0
        except Exception:
            _is_fact = None
        conn.execute("""
            INSERT INTO knowledge_claims
            (claim_hash, hypothesis_text, normalized_text, domain,
             first_experiment_id, last_experiment_id,
             created_at, last_updated_at, claim_type, is_meta, is_empirical_fact)
            VALUES (?, ?, ?, ?, ?, ?,
                    CAST(strftime('%s','now') AS REAL), CAST(strftime('%s','now') AS REAL), ?, ?, ?)
        """, (h_hash, exp['hypothesis'], normalize_hypothesis(exp['hypothesis']),
              exp['domain'], exp['id'], exp['id'], claim_type, _is_meta, _is_fact))
        claim_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(f"Created new claim {claim_id} for {experiment_id} (type={claim_type})")
    else:
        claim_id = claim['id']
        # Update last_experiment_id
        conn.execute("""
            UPDATE knowledge_claims SET last_experiment_id = ?,
                   last_updated_at = CAST(strftime('%s','now') AS REAL)
            WHERE id = ?
        """, (exp['id'], claim_id))
    
    # Attach worker results as evidence
    wrs = conn.execute("""
        SELECT id, hypothesis_supported, supported, key_finding,
               finding, domain, confidence
        FROM worker_results WHERE experiment_id = ?
    """, (experiment_id,)).fetchall()
    
    attached = 0
    if not wrs:
        evidence_type = classify_refutation_type(exp['refutation_type'])
        conn.execute("""
            INSERT OR IGNORE INTO claim_evidence
            (claim_id, experiment_id, evidence_type, domain)
            VALUES (?, ?, ?, ?)
        """, (claim_id, experiment_id, evidence_type, exp['domain']))
        attached = 1
    else:
        for wr in wrs:
            support = extract_support_from_result(wr)
            if support == 1:
                evidence_type = 'support'
            elif support == 0:
                evidence_type = 'refute'
            else:
                evidence_type = classify_refutation_type(exp['refutation_type'])
            
            conn.execute("""
                INSERT OR IGNORE INTO claim_evidence
                (claim_id, experiment_id, worker_result_id, evidence_type,
                 confidence, key_finding, domain)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (claim_id, experiment_id, wr['id'], evidence_type,
                  wr['confidence'], wr['key_finding'] or wr['finding'],
                  wr['domain'] or exp['domain']))
            attached += 1
    
    # Recompute posterior
    counts = conn.execute("""
        SELECT evidence_type, COUNT(*) as cnt
        FROM claim_evidence WHERE claim_id = ?
        GROUP BY evidence_type
    """, (claim_id,)).fetchall()
    
    count_dict = {r['evidence_type']: r['cnt'] for r in counts}
    support = count_dict.get('support', 0)
    refute = count_dict.get('refute', 0)
    boundary = count_dict.get('boundary', 0)
    uncertain = count_dict.get('uncertain', 0)
    total = support + refute + boundary + uncertain
    
    posterior = compute_posterior(support, refute, boundary)
    status = determine_status(posterior, total, support, refute)
    
    # Escalation: check for consecutive refutation streak
    status = check_consecutive_refutation(conn, claim_id, status)
    
    conn.execute("""
        UPDATE knowledge_claims
        SET posterior = ?, support_count = ?, refute_count = ?,
            boundary_count = ?, uncertain_count = ?, total_evidence = ?,
            status = ?, last_updated_at = CAST(strftime('%s','now') AS REAL)
        WHERE id = ?
    """, (posterior, support, refute, boundary, uncertain, total, status, claim_id))

    # Fill the human-readable summary from the best finding (the ANSWER), so the
    # claim stops displaying the question it was created from. Same selector as
    # the one-shot backfill; runs per-result on the live intake path.
    refresh_claim_summary(conn, claim_id, support, refute)

    # Warn when a non-directional claim has a high posterior — the binary
    # support/refute flag is meaningless for open questions, so a high
    # posterior means "N workers found *some* answer" not "N workers found
    # the *same* answer."
    claim_type_row = conn.execute(
        "SELECT claim_type FROM knowledge_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    if claim_type_row and claim_type_row[0] == 'NON_DIRECTIONAL' and posterior > 0.7 and support >= 3:
        print(f"⚠️  NON-DIRECTIONAL claim {claim_id} has posterior={posterior:.3f} "
              f"({support} supports) — binary support/refute may be inflated. "
              f"Check if 'supports' actually agree on magnitude/direction.")
    
    conn.commit()
    
    old_status = claim['id'] if claim else 'NEW'
    if status in (CONTESTED, RETIRED) and old_status not in (CONTESTED, RETIRED):
        print(f"⚠️  Claim {claim_id} transitioned to {status}: {exp['hypothesis'][:80]}")
    
    return claim_id, status, posterior


def list_claims(conn, status_filter=None):
    """List claims with their status and evidence counts."""
    query = "SELECT * FROM knowledge_claims"
    params = []
    
    if status_filter:
        query += " WHERE status = ?"
        params.append(status_filter)
    
    query += " ORDER BY total_evidence DESC, posterior DESC"
    
    claims = conn.execute(query, params).fetchall()
    
    print(f"{'ID':>5} {'Status':<12} {'Post':>5} {'S':>4} {'R':>4} {'B':>4} {'Tot':>4} {'Finding'}")
    print("-" * 100)

    for c in claims:
        # prefer the finding (claim_summary) over the prompt (hypothesis_text)
        keys = c.keys() if hasattr(c, 'keys') else []
        text = (c['claim_summary'] if 'claim_summary' in keys and c['claim_summary']
                else c['hypothesis_text']) or ''
        print(f"{c['id']:>5} {c['status']:<12} {c['posterior']:.3f} {c['support_count']:>4} "
              f"{c['refute_count']:>4} {c['boundary_count']:>4} {c['total_evidence']:>4} {text[:60]}")
    
    print(f"\nTotal: {len(claims)} claims")


def show_claim_status(conn, claim_id):
    """Show detailed status for a single claim."""
    claim = conn.execute(
        "SELECT * FROM knowledge_claims WHERE id = ?", (claim_id,)
    ).fetchone()
    
    if not claim:
        print(f"Claim {claim_id} not found")
        return
    
    print(f"\nClaim #{claim['id']}: {claim['status']}")
    keys = claim.keys() if hasattr(claim, 'keys') else []
    if 'claim_summary' in keys and claim['claim_summary']:
        print(f"Finding:    {claim['claim_summary']}")
    print(f"Question:   {claim['hypothesis_text']}")
    print(f"Domain: {claim['domain']}")
    print(f"Posterior: {claim['posterior']:.3f}")
    print(f"Evidence: {claim['support_count']} support, {claim['refute_count']} refute, "
          f"{claim['boundary_count']} boundary, {claim['uncertain_count']} uncertain")
    print(f"Total: {claim['total_evidence']} evidence entries")
    print(f"First experiment: {claim['first_experiment_id']}")
    print(f"Last experiment: {claim['last_experiment_id']}")
    print(f"Created: {claim['created_at']}")
    print(f"Last updated: {claim['last_updated_at']}")
    
    # Show evidence entries
    evidence = conn.execute("""
        SELECT ce.*, e.refutation_type
        FROM claim_evidence ce
        LEFT JOIN experiments e ON e.id = ce.experiment_id
        WHERE ce.claim_id = ?
        ORDER BY ce.attached_at
    """, (claim_id,)).fetchall()
    
    if evidence:
        print(f"\nEvidence ({len(evidence)} entries):")
        for ev in evidence:
            finding_short = (ev['key_finding'] or '')[:80]
            print(f"  [{ev['evidence_type']}] {ev['experiment_id']} — {finding_short}")


def show_summary(conn):
    """Show aggregate statistics."""
    stats = conn.execute("""
        SELECT 
            COUNT(*) as total_claims,
            SUM(CASE WHEN status='ACTIVE' THEN 1 ELSE 0 END) as active,
            SUM(CASE WHEN status='CONDITIONAL' THEN 1 ELSE 0 END) as conditional,
            SUM(CASE WHEN status='CONTESTED' THEN 1 ELSE 0 END) as contested,
            SUM(CASE WHEN status='RETIRED' THEN 1 ELSE 0 END) as retired,
            SUM(CASE WHEN status='UNTESTED' THEN 1 ELSE 0 END) as untested,
            AVG(posterior) as avg_posterior,
            AVG(total_evidence) as avg_evidence
        FROM knowledge_claims
    """).fetchone()
    
    print(f"\nKnowledge Claim Summary")
    print(f"{'='*40}")
    print(f"Total claims:      {stats['total_claims']}")
    print(f"  ACTIVE:          {stats['active']}")
    print(f"  CONDITIONAL:     {stats['conditional']}")
    print(f"  CONTESTED:       {stats['contested']}")
    print(f"  RETIRED:         {stats['retired']}")
    print(f"  UNTESTED:        {stats['untested']}")
    print(f"Avg posterior:     {stats['avg_posterior']:.3f}")
    print(f"Avg evidence/claim: {stats['avg_evidence']:.1f}")
    
    # Top contested
    contested = conn.execute("""
        SELECT id, posterior, total_evidence, hypothesis_text
        FROM knowledge_claims
        WHERE status = 'CONTESTED'
        ORDER BY total_evidence DESC
        LIMIT 5
    """).fetchall()
    
    if contested:
        print(f"\nTop contested claims:")
        for c in contested:
            print(f"  #{c['id']} (p={c['posterior']:.3f}, n={c['total_evidence']}): {c['hypothesis_text'][:70]}")
    
    # Top retired
    retired = conn.execute("""
        SELECT id, posterior, total_evidence, hypothesis_text
        FROM knowledge_claims
        WHERE status = 'RETIRED'
        ORDER BY total_evidence DESC
        LIMIT 5
    """).fetchall()
    
    if retired:
        print(f"\nTop retired claims:")
        for c in retired:
            print(f"  #{c['id']} (p={c['posterior']:.3f}, n={c['total_evidence']}): {c['hypothesis_text'][:70]}")
    
    # Highest confidence
    confident = conn.execute("""
        SELECT id, posterior, total_evidence, hypothesis_text
        FROM knowledge_claims
        WHERE status = 'ACTIVE' AND total_evidence >= 3
        ORDER BY posterior DESC
        LIMIT 5
    """).fetchall()
    
    if confident:
        print(f"\nMost confident claims:")
        for c in confident:
            print(f"  #{c['id']} (p={c['posterior']:.3f}, n={c['total_evidence']}): {c['hypothesis_text'][:70]}")


def get_claim_status_for_hypothesis(conn, hypothesis_text):
    """Look up claim status for a hypothesis. Used by batch_create_tasks.py.
    
    Returns: dict with status, claim_status, posterior, support_count, refute_count, claim_id
             or None if no claim found.
    
    status (old): ACTIVE/CONDITIONAL/CONTESTED/RETIRED/UNTESTED
    claim_status (new): CANDIDATE/REPLICATED/DISPUTED/ESTABLISHED/HISTORICAL_UNKNOWN
    """
    h_hash = hypothesis_hash(hypothesis_text)
    claim = conn.execute(
        "SELECT * FROM knowledge_claims WHERE claim_hash = ?", (h_hash,)
    ).fetchone()
    
    if not claim:
        return None
    
    return {
        'claim_id': claim['id'],
        'status': claim['status'],
        'claim_status': claim['claim_status'] if 'claim_status' in claim.keys() else None,
        'posterior': claim['posterior'],
        'support_count': claim['support_count'],
        'refute_count': claim['refute_count'],
        'boundary_count': claim['boundary_count'],
        'total_evidence': claim['total_evidence'],
        'hypothesis_text': claim['hypothesis_text'],
    }


def get_retired_claims_hashes(conn):
    """Return set of claim hashes that are RETIRED. Used by task creation to block."""
    rows = conn.execute(
        "SELECT claim_hash FROM knowledge_claims WHERE status = 'RETIRED'"
    ).fetchall()
    return {r['claim_hash'] for r in rows}


def get_contested_claims_hashes(conn):
    """Return set of claim hashes that are CONTESTED. Used for warnings."""
    rows = conn.execute(
        "SELECT claim_hash FROM knowledge_claims WHERE status = 'CONTESTED'"
    ).fetchall()
    return {r['claim_hash'] for r in rows}


def main():
    parser = argparse.ArgumentParser(description="Knowledge claim lifecycle management")
    parser.add_argument("action", choices=[
        "init", "populate", "attach", "attach-all", "posterior", "posterior-all",
        "list", "status", "retired", "summary"
    ])
    parser.add_argument("target", nargs="?", help="Experiment ID or Claim ID")
    parser.add_argument("--status", help="Filter by status")
    
    args = parser.parse_args()
    conn = get_db()
    
    try:
        if args.action == "init":
            init_schema(conn)
        
        elif args.action == "populate":
            init_schema(conn)  # Ensure tables exist
            populate_claims(conn)
        
        elif args.action == "attach":
            if not args.target:
                print("Usage: claim_lifecycle.py attach <experiment_id>")
                return
            attach_experiment(conn, args.target)
        
        elif args.action == "attach-all":
            # Attach all experiments not yet in claim_evidence
            unattached = conn.execute("""
                SELECT e.id FROM experiments e
                WHERE e.hypothesis IS NOT NULL AND length(e.hypothesis) > 10
                AND e.id NOT IN (SELECT DISTINCT experiment_id FROM claim_evidence)
                ORDER BY e.created_at ASC
            """).fetchall()
            
            print(f"Attaching {len(unattached)} unattached experiments...")
            for i, row in enumerate(unattached):
                attach_experiment(conn, row['id'])
                if (i + 1) % 1000 == 0:
                    print(f"  ... {i+1}/{len(unattached)}")
            print(f"Done. Attached {len(unattached)} experiments.")
        
        elif args.action == "posterior":
            if not args.target:
                print("Usage: claim_lifecycle.py posterior <claim_id>")
                return
            # Recompute single claim
            claim_id = int(args.target)
            counts = conn.execute("""
                SELECT evidence_type, COUNT(*) as cnt
                FROM claim_evidence WHERE claim_id = ?
                GROUP BY evidence_type
            """, (claim_id,)).fetchall()
            
            count_dict = {r['evidence_type']: r['cnt'] for r in counts}
            support = count_dict.get('support', 0)
            refute = count_dict.get('refute', 0)
            boundary = count_dict.get('boundary', 0)
            total = sum(count_dict.values())
            
            posterior = compute_posterior(support, refute, boundary)
            status = determine_status(posterior, total, support, refute)
            
            conn.execute("""
                UPDATE knowledge_claims
                SET posterior=?, support_count=?, refute_count=?,
                    boundary_count=?, total_evidence=?, status=?,
                    last_updated_at=CAST(strftime('%s','now') AS REAL)
                WHERE id=?
            """, (posterior, support, refute, boundary, total, status, claim_id))
            conn.commit()
            print(f"Claim {claim_id}: posterior={posterior:.3f}, status={status}")
        
        elif args.action == "posterior-all":
            compute_all_posteriors(conn)
        
        elif args.action == "list":
            list_claims(conn, args.status)
        
        elif args.action == "status":
            if not args.target:
                print("Usage: claim_lifecycle.py status <claim_id>")
                return
            show_claim_status(conn, int(args.target))
        
        elif args.action == "retired":
            list_claims(conn, "RETIRED")
        
        elif args.action == "summary":
            show_summary(conn)
    
    finally:
        conn.close()


if __name__ == "__main__":
    main()
