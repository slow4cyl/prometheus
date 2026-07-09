#!/usr/bin/env python3
"""
Replication Tracker — compares validation results to original findings.

Purpose: Track whether validated findings replicate. Surface disagreements.
Compute replication probability per finding.

Usage:
    python3 replication_tracker.py [--dry-run] [--report]
"""

import sqlite3
import os
import re
import sys
import time
import json
import fcntl
import subprocess
import db_retry

DB_PATH = os.path.expanduser('~/.hermes/prometheus.db')
KANBAN_DB = os.path.expanduser('~/.hermes/kanban.db')
LOCK_FILE = os.path.expanduser('~/.hermes/replication_tracker.lock')
STATE_FILE = os.path.expanduser('~/.hermes/replication_state.json')


def acquire_lock():
    fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except IOError:
        fd.close()
        return None


def release_lock(fd):
    if fd:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def get_db():
    return db_retry.get_db(DB_PATH)


def get_kanban_db():
    return db_retry.get_db(KANBAN_DB)


def find_completed_validations(prom_conn, kanban_conn):
    """Find validation tasks that have completed and need comparison."""
    # Get pending validation records
    pending = prom_conn.execute('''
        SELECT id, original_experiment_id, validation_task_id, 
               original_finding, original_confidence
        FROM replication_results
        WHERE replication_status = 'pending'
        AND validation_task_id IS NOT NULL
    ''').fetchall()
    
    completed = []
    for row in pending:
        record_id = row[0]
        exp_id = row[1]
        task_id = row[2]
        original_finding = row[3]
        original_confidence = row[4]
        
        # Check if task completed OR if worker_results exist
        task = kanban_conn.execute(
            "SELECT status, result FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        
        task_completed = task and task[0] == 'completed'
        
        # Also check if worker_results exist (workers may write results without completing kanban)
        val_result = prom_conn.execute('''
            SELECT hypothesis_supported, confidence, key_finding
            FROM worker_results
            WHERE experiment_id = ? AND kanban_task_id = ?
            ORDER BY created_at DESC LIMIT 1
        ''', (exp_id, task_id)).fetchone()
        
        has_result = val_result is not None
        
        if task_completed or has_result:
            
            if val_result:
                completed.append({
                    'record_id': record_id,
                    'experiment_id': exp_id,
                    'original_finding': original_finding,
                    'original_confidence': original_confidence,
                    'validation_supported': val_result[0],
                    'validation_confidence': float(val_result[1]) if val_result[1] else 0,
                    'validation_finding': val_result[2],
                })
    
    return completed


def _is_original_confirmed(original_finding):
    """Parse the original finding to determine if the hypothesis was supported.
    
    Returns True if the finding indicates the hypothesis was confirmed/supported,
    False if refuted, None if indeterminate.
    """
    if not original_finding:
        return None
    text = original_finding.strip().upper()
    # Confirmed/supported patterns
    if text.startswith('CONFIRMED:') or text.startswith('SUPPORTED:'):
        return True
    # Refuted patterns
    if text.startswith('REFUTED:') or text.startswith('REJECTED:'):
        return False
    # Partially confirmed — treat as confirmed (weaker)
    if text.startswith('PARTIALLY CONFIRMED') or text.startswith('PARTIALLY SUPPORTED'):
        return True
    # Weak rejection — treat as refuted
    if text.startswith('WEAK REJECTION') or text.startswith('WEAK REFUTATION'):
        return False
    # Adversarial patterns — check if the attack failed (confirms original) or succeeded
    if 'ADVERSARIAL CONFIRMATION' in text or 'ADVERSARIAL ATTACK FAILED' in text:
        return True
    if 'ADVERSARIAL ATTACK SUCCEEDED' in text or 'ADVERSARIAL ATTACK PARTIALLY SUCCEEDED' in text:
        return False
    return None


def compare_results(original, validation):
    """Compare original and validation results.
    
    Returns:
        'replicated' — both confirm, similar confidence
        'partial' — both confirm but confidence differs significantly
        'disagreed' — one confirms, one refutes
        'weak_replication' — both confirm but low confidence
    """
    orig_supported = _is_original_confirmed(original.get('original_finding', ''))
    if orig_supported is None:
        orig_supported = True  # Default to True if we can't parse (backward compat)
    
    val_supported = bool(validation['validation_supported'])
    
    # Both agree on direction: both confirmed or both refuted
    if orig_supported == val_supported:
        # Both refuted — that's a replication (both agree hypothesis is false)
        if not orig_supported and not val_supported:
            return 'replicated'
        
        # Both confirmed — check confidence similarity
        orig_conf = original['original_confidence']
        val_conf = validation['validation_confidence']
        conf_diff = abs(orig_conf - val_conf)
        if conf_diff < 0.15:
            return 'replicated'
        elif orig_conf > 0.9 and val_conf < 0.6:
            return 'partial'  # Original was confident, validation less so
        elif val_conf < 0.5:
            return 'weak_replication'
        else:
            return 'replicated'
    
    # Disagree on direction
    return 'disagreed'


# ── Break categorization (boundary engine) ──────────────────────────

STRUCTURAL_KEYWORDS = [
    # Precondition violations: claim holds in restricted case, fails generally
    r'(only\s+(holds|applies|works)|restricted\s+to|in\s+the\s+special\s+case)',
    r'(when|if)\s+[^.]+(is\s+(stationary|fixed|constant|unchanged|static))',
    r'(breaks|fails|violated)\s+(when|if|under|for)',
    r'(does\s+not\s+(generalize|transfer|extend))',
    r'(assumes?|assumption|requires?|requirement|precondition)',
    # Restless/regenerating patterns
    r'(restless|regenerat\w+|non.?stationary|time.?varying)',
    # Category error patterns
    r'(different\s+(mechanism|category|structure|kind|type))',
    r'(not\s+(a|an)\s+(\w+\s+)?(isomorphism|analog|transfer))',
    r'(superficial|surface|apparent|cosmetic)\s+(match|similarity|analog)',
    # Competition/strategic patterns
    r'(strategic|game.?theoretic|competitive|multi.?agent|Nash|equilibrium)',
    # Survival/absorbing state patterns
    r'(bankruptcy|ruin|absorbing\s+state|survival|extinction)',
    # Endogenous action space patterns
    r'(new\s+(arms?|actions?|options?|programs?|paradigms?)\s+(emerge|appear|created))',
    r'(action\s+space\s+(grows?|expands?|changes?|is\s+not\s+fixed))',
]


def categorize_break(original_finding, validation_finding, original_domain):
    """Categorize a disagreement by the type of break it reveals.
    
    Returns one of:
      'precondition_violation' — claim held in restricted case, fails generally
      'category_error' — domains are different in kind, not just parameters
      'magnitude_only' — same direction, different magnitude
      'direction_reversal' — opposite sign/direction
      'analytic_error' — original contained unverified limit/convergence claim
      'uncategorized' — doesn't match known patterns
    """
    if original_finding is None:
        original_finding = ''
    if validation_finding is None:
        validation_finding = ''
    
    combined = (original_finding + ' ' + validation_finding).lower()
    
    # Check for structural precondition keywords
    structural_score = 0
    for pattern in STRUCTURAL_KEYWORDS:
        if re.search(pattern, combined):
            structural_score += 1
    
    if structural_score >= 2:
        return 'precondition_violation'
    elif structural_score >= 1:
        return 'category_error'
    
    # Check for direction reversal (opposite signs)
    reversal_patterns = [
        r'(opposite\s+(direction|sign|effect|result))',
        r'(reverses?|flips?|inverts?)\s+(direction|sign)',
        r'(negative|positive)\s+instead\s+of\s+(positive|negative)',
    ]
    for pattern in reversal_patterns:
        if re.search(pattern, combined):
            return 'direction_reversal'
    
    # Default: magnitude difference
    return 'magnitude_only'


def compute_break_informativeness(break_category, original_confidence):
    """Score how informative a break is for boundary finding.
    
    precondition_violation: 15-20 (reveals where invariants stop being true)
    category_error:          10-15 (different mechanism, not just parameters)
    direction_reversal:       8-12 (sign flip indicates deeper structural issue)
    magnitude_only:           3-8  (same story, different magnitude — less informative)
    analytic_error:           5-10 (methodological, not structural, but still useful)
    """
    base = {
        'precondition_violation': 17,
        'category_error': 13,
        'direction_reversal': 10,
        'magnitude_only': 5,
        'analytic_error': 7,
        'uncategorized': 5,
    }.get(break_category, 5)
    
    # Boost if original was overconfident (break more informative)
    if original_confidence and original_confidence > 0.85:
        base += 2
    
    return min(20, max(0, base))


def verify_analytic_claim(original_finding):
    """Run analytic_verifier.py on the original finding to detect
    unverified mathematical claims."""
    import subprocess
    
    if not original_finding:
        return None
    
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'analytic_verifier.py')
        r = subprocess.run(
            [sys.executable, script, '--finding', original_finding],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout.strip())
    except Exception:
        pass
    
    return None


def update_record(prom_conn, record_id, status, validation,
                  break_informativeness=0, analytic_verdict=''):
    """Update replication_results with comparison outcome including
    boundary-engine metadata."""
    prom_conn.execute('''
        UPDATE replication_results
        SET replication_status = ?,
            validation_finding = ?,
            validation_confidence = ?,
            validation_hypothesis_supported = ?,
            validated_at = ?,
            break_informativeness = ?,
            analytic_verdict = ?
        WHERE id = ?
    ''', (
        status,
        validation['validation_finding'],
        validation['validation_confidence'],
        validation['validation_supported'],
        int(time.time()),
        break_informativeness,
        analytic_verdict,
        record_id
    ))
    prom_conn.commit()


def compute_replication_stats(prom_conn):
    """Compute overall replication statistics."""
    rows = prom_conn.execute('''
        SELECT replication_status, COUNT(*) as cnt
        FROM replication_results
        WHERE replication_status != 'pending'
        GROUP BY replication_status
    ''').fetchall()
    
    stats = {r[0]: r[1] for r in rows}
    total = sum(stats.values())
    
    if total == 0:
        return None
    
    replicated = stats.get('replicated', 0)
    partial = stats.get('partial', 0)
    weak = stats.get('weak_replication', 0)
    disagreed = stats.get('disagreed', 0)
    
    return {
        'total_validated': total,
        'replicated': replicated,
        'partial': partial,
        'weak_replication': weak,
        'disagreed': disagreed,
        'replication_rate': replicated / total if total > 0 else 0,
        'survival_rate': (replicated + partial + weak) / total if total > 0 else 0,
    }


def save_state(stats, prom_conn=None):
    """Save replication state for monitoring and scorer feedback."""
    state = {
        'last_updated': int(time.time()),
        'stats': stats,
    }
    
    # Add domain disagreement rates and disagreed experiment IDs for scorer
    if prom_conn:
        try:
            rows = prom_conn.execute('''
                SELECT original_domain, 
                       SUM(CASE WHEN replication_status='disagreed' THEN 1 ELSE 0 END) as disagreed,
                       COUNT(*) as total
                FROM replication_results
                WHERE replication_status != 'pending'
                GROUP BY original_domain
            ''').fetchall()
            
            domain_rates = {}
            for r in rows:
                if r[2] >= 2:
                    domain_rates[r[0] or 'unknown'] = {
                        'disagreement_rate': round(r[1] / r[2], 3),
                        'replicated': r[2] - r[1],
                        'disagreed': r[1],
                        'total': r[2]
                    }
            state['domain_disagreement_rates'] = domain_rates
            
            disagreed_ids = [r[0] for r in prom_conn.execute(
                "SELECT original_experiment_id FROM replication_results WHERE replication_status='disagreed'"
            ).fetchall()]
            state['disagreed_experiment_ids'] = disagreed_ids
            
            # ── Boundary engine metadata ──
            break_rows = prom_conn.execute('''
                SELECT original_domain,
                       replication_status,
                       break_informativeness,
                       analytic_verdict
                FROM replication_results
                WHERE replication_status != 'pending'
                AND break_informativeness > 0
            ''').fetchall()
            
            domain_break_scores = {}
            for r in break_rows:
                dom = r[0] or 'unknown'
                bi = r[2] or 0
                if dom not in domain_break_scores:
                    domain_break_scores[dom] = {
                        'total_break_score': 0,
                        'break_count': 0,
                        'avg_informativeness': 0,
                    }
                domain_break_scores[dom]['total_break_score'] += bi
                domain_break_scores[dom]['break_count'] += 1
            
            for dom in domain_break_scores:
                bd = domain_break_scores[dom]
                bd['avg_informativeness'] = round(
                    bd['total_break_score'] / max(bd['break_count'], 1), 1
                )
            
            state['domain_break_informativeness'] = domain_break_scores
            
            # Count conjectured (unverified limit) claims
            conjectured_count = prom_conn.execute(
                "SELECT COUNT(*) FROM replication_results "
                "WHERE analytic_verdict = 'conjectured'"
            ).fetchone()[0]
            state['conjectured_claims'] = conjectured_count
            
            # Track conjectured claims by domain
            conj_rows = prom_conn.execute(
                "SELECT original_domain, COUNT(*) as cnt "
                "FROM replication_results "
                "WHERE analytic_verdict = 'conjectured' "
                "GROUP BY original_domain"
            ).fetchall()
            state['conjectured_domains'] = {
                r[0] or 'unknown': r[1] for r in conj_rows
            }
            
        except Exception:
            pass
    
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Track replication results')
    parser.add_argument('--dry-run', action='store_true', help='Preview without updating')
    parser.add_argument('--report', action='store_true', help='Print report and exit')
    args = parser.parse_args()
    
    lock_fd = acquire_lock()
    if not lock_fd:
        print('[Replication] Another instance running, skipping.')
        return
    
    try:
        prom_conn = get_db()
        kanban_conn = get_kanban_db()
        
        # Find completed validations
        completed = find_completed_validations(prom_conn, kanban_conn)
        
        if not completed and not args.report:
            print('[Replication] No completed validations to process.')

            # Still show stats
            stats = compute_replication_stats(prom_conn)
            if stats:
                print('[Replication] Current stats: {}/{} replicated ({:.1f}%)'.format(
                    stats['replicated'], stats['total_validated'],
                    100 * stats['replication_rate']))
            # IMPORTANT: refresh the state snapshot even when there are no NEW
            # validations. save_state() recomputes domain disagreement / break /
            # conjectured aggregations from the EXISTING replication_results rows and
            # restamps last_updated — all valid to recompute anytime. Skipping it here
            # (the old behavior) let replication_state.json silently go stale for days
            # whenever validations were sparse, while this cron kept reporting "ok"
            # (the silent-success / liveness-not-content trap). curiosity_scorer now
            # detects that staleness and falls back to the live DB, but the snapshot
            # should simply stay fresh. (2026-06-25)
            if stats and not args.dry_run:
                save_state(stats, prom_conn)
            return
        
        # Process completed validations
        updated = 0
        disagreements = []
        
        for c in completed:
            status = compare_results(
                {'original_confidence': c['original_confidence'],
                 'original_finding': c.get('original_finding', '')},
                c
            )
            
            print('  {} -> {}'.format(c['experiment_id'], status))
            print('    Original conf: {:.2f} | Validation conf: {:.2f} | Supported: {}'.format(
                c['original_confidence'], c['validation_confidence'],
                c['validation_supported']))
            
            break_info = 0
            analytic_verdict = ''
            original_finding = c.get('original_finding', '')
            validation_finding = c.get('validation_finding', '')
            
            if status == 'disagreed':
                # Categorize the break type
                break_category = categorize_break(
                    original_finding, validation_finding, ''
                )
                break_info = compute_break_informativeness(
                    break_category, c['original_confidence']
                )
                print('    Break category: {} (informativens={})'.format(
                    break_category, break_info))
                
                # Run analytic verification on the original finding
                analytic_result = verify_analytic_claim(original_finding)
                if analytic_result and analytic_result.get('has_limit_claim'):
                    if analytic_result.get('verdict') == 'conjectured':
                        analytic_verdict = 'conjectured'
                        print('    ANALYTIC ISSUE: {}'.format(
                            analytic_result.get('details', '')[:120]))
                    else:
                        analytic_verdict = analytic_result.get('verdict', '')
                else:
                    analytic_verdict = 'no_limits_detected'
                
                disagreements.append(c)
            
            if not args.dry_run:
                update_record(prom_conn, c['record_id'], status, c,
                              break_informativeness=break_info,
                              analytic_verdict=analytic_verdict)
                updated += 1
        
        if not args.dry_run:
            prom_conn.commit()
        
        # Compute and report stats
        stats = compute_replication_stats(prom_conn)
        
        if stats:
            print()
            print('[Replication] === REPLICATION REPORT ===')
            print('  Total validated: {}'.format(stats['total_validated']))
            print('  Replicated: {} ({:.1f}%)'.format(
                stats['replicated'], 100 * stats['replication_rate']))
            print('  Partial: {}'.format(stats['partial']))
            print('  Weak: {}'.format(stats['weak_replication']))
            print('  DISAGREED: {} ({:.1f}%)'.format(
                stats['disagreed'], 100 * stats['disagreed'] / stats['total_validated'] if stats['total_validated'] > 0 else 0))
            print('  Survival rate: {:.1f}%'.format(100 * stats['survival_rate']))
            
            if not args.dry_run:
                save_state(stats, prom_conn)
        
        # Surface disagreements
        if disagreements:
            print()
            print('[Replication] === DISAGREEMENTS (attention needed) ===')
            for d in disagreements:
                print('  {} (domain: {})'.format(d['experiment_id'], 
                    prom_conn.execute('SELECT original_domain FROM replication_results WHERE original_experiment_id = ?', 
                        (d['experiment_id'],)).fetchone()[0] or ''))
                print('    Original confidence: {:.2f}'.format(d['original_confidence']))
                print('    Validation: supported={} conf={:.2f}'.format(
                    d['validation_supported'], d['validation_confidence']))
                print('    Finding: {}'.format((d['validation_finding'] or '')[:200]))
                print()
        
        print('[Replication] Updated {} records.'.format(updated))
        
        prom_conn.close()
        kanban_conn.close()
        
    finally:
        release_lock(lock_fd)


if __name__ == '__main__':
    main()
