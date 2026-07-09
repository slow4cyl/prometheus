#!/usr/bin/env python3
"""
Curiosity Structural Validator and Repair Layer
================================================
Validates curiosity text for structural completeness and repairs
incomplete curiosities by adding missing structural elements.

Structural completeness = presence of:
  1. Source domain
  2. Target domain
  3. Mechanism / transfer claim
  4. Testable prediction / success criterion

Templates supported:
  - Transfer: mechanism M transfers from domain A to domain B
  - Boundary: under what conditions does mechanism M fail
  - Threshold: does threshold T generalize across domains
  - Contrast: why does mechanism M appear in A but not B

Usage:
    python3 curiosity_repair.py "How does X influence Y?"
    python3 curiosity_repair.py --check "Does X transfer from A to B?"
    python3 curiosity_repair.py --batch-input curiosities.json
"""

import re
import json
import sys
import os
import sqlite3
import time

# Structural field patterns
SOURCE_DOMAIN_PATTERNS = [
    r'from\s+(\w[\w\s]*?)(?:\s+to|\s+in|\s+for|\s+across|\s*[\?\.,])',
    r'\[TRANSFER\s+from\s+(\w+)\]',
    r'observed\s+in\s+(\w[\w\s]*?)(?:\s+and|\s+but|\s*,)',
    r'(\w+)\s+domain',
    r'in\s+(\w+)\s+(?:data|results|experiments|studies)',
]

TARGET_DOMAIN_PATTERNS = [
    r'(?:to|in|for|across)\s+(\w[\w\s]*?)(?:\s*[\?\.,]|$)',
    r'apply\s+to\s+(\w[\w\s]*?)(?:\s*[\?\.,]|$)',
    r'transfer\s+(?:to|into)\s+(\w[\w\s]*?)(?:\s*[\?\.,]|$)',
    r'generalize\s+(?:to|across|in)\s+(\w[\w\s]*?)(?:\s*[\?\.,]|$)',
]

MECHANISM_PATTERNS = [
    r'mechanism\s+(?:of|for|behind|underlying)',
    r'(?:does|will|can)\s+(\w[\w\s]*?)\s+transfer',
    r'(?:because|due to|caused by|driven by|results from)',
    r'(?:apply|generalize|extend)\s+(?:to|across)',
    r'(?:power.?law|phase.?transition|threshold|ceiling|saturation|reversal|asymmetry)',
    r'(?:robustness|calibration|information|entropy|spectral|manifold)',
]

PREDICTION_PATTERNS = [
    r'(?:will|should|would|expect|predict|anticipate)',
    r'(?:plateau|increase|decrease|improve|degrade|fail|break|saturate)',
    r'(?:F1|AUC|accuracy|precision|recall|loss|error|rate)',
    r'(?:threshold|ceiling|floor|limit|boundary)',
    r'if\s+.*then',
    r'(?:success|failure)\s+criterion',
    r'\d+\.?\d*\s*(?:%|pp|x|fold|times)',
]


def extract_structural_fields(text):
    """Extract structural fields from curiosity text."""
    t = text.lower()
    
    fields = {
        'source_domain': None,
        'target_domain': None,
        'mechanism': False,
        'prediction': False,
        'transfer_tag': False,
        'has_number': False,
    }
    
    # Check for [TRANSFER from X] tag
    tag_match = re.search(r'\[TRANSFER\s+from\s+(\w+)\]', text, re.IGNORECASE)
    if tag_match:
        fields['transfer_tag'] = True
        fields['source_domain'] = tag_match.group(1)
    
    # Extract source domain from text
    if not fields['source_domain']:
        for pattern in SOURCE_DOMAIN_PATTERNS:
            match = re.search(pattern, t, re.IGNORECASE)
            if match:
                domain = match.group(1).strip()
                if len(domain) > 2 and domain not in ('the', 'this', 'that', 'does', 'can', 'how', 'what'):
                    fields['source_domain'] = domain
                    break
    
    # Extract target domain
    for pattern in TARGET_DOMAIN_PATTERNS:
        match = re.search(pattern, t, re.IGNORECASE)
        if match:
            domain = match.group(1).strip()
            if len(domain) > 2 and domain not in ('the', 'this', 'that', 'does', 'can', 'how', 'what'):
                fields['target_domain'] = domain
                break
    
    # Check for mechanism language
    for pattern in MECHANISM_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            fields['mechanism'] = True
            break
    
    # Check for prediction language
    for pattern in PREDICTION_PATTERNS:
        if re.search(pattern, t, re.IGNORECASE):
            fields['prediction'] = True
            break
    
    # Check for numbers
    fields['has_number'] = bool(re.search(r'\d+', text))
    
    return fields


def compute_completeness(fields):
    """Compute structural completeness score (0-4)."""
    score = 0
    if fields['source_domain']:
        score += 1
    if fields['target_domain']:
        score += 1
    if fields['mechanism']:
        score += 1
    if fields['prediction']:
        score += 1
    return score


def detect_template(text):
    """Detect which hypothesis template the curiosity fits."""
    t = text.lower()
    
    if '[transfer' in t or 'transfer' in t and ('from' in t or 'to' in t):
        return 'transfer'
    if any(w in t for w in ['fail', 'break', 'boundary', 'limit', 'except', 'under what']):
        return 'boundary'
    if any(w in t for w in ['threshold', 'ceiling', 'saturation', 'plateau', 'generaliz']):
        return 'threshold'
    if any(w in t for w in ['why', 'contrast', 'differ', 'asymmetr', 'unlike']):
        return 'contrast'
    if any(w in t for w in ['how does', 'influence', 'effect', 'impact']):
        return 'open_investigation'
    
    return 'unknown'


def repair_curiosity(text, fields, template, synthesis_context=None):
    """Repair a curiosity by adding missing structural elements."""
    
    # Extract what we can from the text
    t = text.lower()
    
    # Try to infer source domain from context
    source = fields.get('source_domain')
    target = fields.get('target_domain')
    
    # Try to extract mechanism from text
    mechanism = None
    mechanism_phrases = [
        r'(?:the\s+)?(.+?)\s+(?:mechanism|effect|pattern|phenomenon)',
        r'(?:does|will)\s+(.+?)\s+transfer',
        r'(?:apply|generalize)\s+(.+?)\s+(?:to|across)',
    ]
    for pattern in mechanism_phrases:
        match = re.search(pattern, t)
        if match:
            mechanism = match.group(1).strip()
            if len(mechanism) > 3:
                break
    
    if not mechanism:
        # Use a generic mechanism reference
        mechanism = "the observed mechanism"
    
    # Determine template type and repair
    if template == 'transfer':
        if not fields['source_domain']:
            # No source domain — reclassify as open investigation
            # Using 'transfer from unknown_domain' creates malformed placeholder tasks
            repaired = text
            if not fields['prediction']:
                repaired += " — will this produce measurable effects?"
        elif not fields['target_domain']:
            repaired = f"[TRANSFER from {source}] Does {mechanism} apply to new domains beyond {source}?"
        elif not fields['prediction']:
            repaired = f"[TRANSFER from {source}] Does {mechanism} transfer from {source} to {target} — will performance metrics improve?"
        else:
            repaired = text  # Already complete
    
    elif template == 'boundary':
        # Preserve original text if it already has mechanism — just add missing fields
        if not fields['source_domain'] and not fields['target_domain']:
            # Keep the original mechanism reference, just add structure
            repaired = text  # Boundary curiosities are often fine as-is
            if not fields['prediction']:
                repaired = text + " — what measurable outcomes indicate failure?"
        elif not fields['prediction']:
            repaired = text + " — what measurable outcomes indicate failure?"
        else:
            repaired = text
    
    elif template == 'threshold':
        if not fields['target_domain']:
            repaired = f"Does the threshold for {mechanism} generalize across domains — is there a universal threshold?"
        elif not fields['prediction']:
            repaired = f"Does the threshold for {mechanism} generalize from {source} to {target}?"
        else:
            repaired = text
    
    elif template == 'contrast':
        if not fields['target_domain']:
            repaired = f"Why does {mechanism} appear in {source} but not in other domains?"
        elif not fields['prediction']:
            repaired = f"Why does {mechanism} appear in {source} but not in {target} — what makes {source} different?"
        else:
            repaired = text
    
    else:
        # Open investigation — try to add structure
        if not fields['source_domain'] and not fields['target_domain']:
            # No source or target domain — keep as-is, don't fabricate placeholder domains
            repaired = text
            if not fields['prediction']:
                repaired += " — will this produce measurable effects?"
        else:
            repaired = text
    
    return repaired


def validate_and_repair(text, synthesis_context=None):
    """Validate and repair a single curiosity. Returns dict with results."""
    fields = extract_structural_fields(text)
    completeness = compute_completeness(fields)
    template = detect_template(text)
    
    result = {
        'original': text,
        'fields': fields,
        'completeness': completeness,
        'template': template,
        'repaired': None,
        'repaired_completeness': None,
        'needs_repair': completeness < 3,
    }
    
    if result['needs_repair']:
        repaired = repair_curiosity(text, fields, template, synthesis_context)
        repaired_fields = extract_structural_fields(repaired)
        result['repaired'] = repaired
        result['repaired_completeness'] = compute_completeness(repaired_fields)
        result['repaired_fields'] = repaired_fields
    
    return result


def format_report(results):
    """Format validation results as readable report."""
    lines = []
    lines.append("=" * 60)
    lines.append("CURIOSITY STRUCTURAL VALIDATION")
    lines.append("=" * 60)
    
    for i, r in enumerate(results):
        lines.append(f"\n--- Curiosity {i+1} ---")
        lines.append(f"Text: {r['original'][:120]}...")
        lines.append(f"Template: {r['template']}")
        lines.append(f"Completeness: {r['completeness']}/4")
        lines.append(f"Fields: source={r['fields']['source_domain']}, "
                     f"target={r['fields']['target_domain']}, "
                     f"mechanism={r['fields']['mechanism']}, "
                     f"prediction={r['fields']['prediction']}")
        
        if r['needs_repair']:
            lines.append(f"NEEDS REPAIR")
            if r['repaired']:
                lines.append(f"Repaired: {r['repaired'][:120]}...")
                lines.append(f"Repaired completeness: {r['repaired_completeness']}/4")
        else:
            lines.append(f"COMPLETE — no repair needed")
    
    # Summary
    n = len(results)
    complete = sum(1 for r in results if not r['needs_repair'])
    repaired = sum(1 for r in results if r['needs_repair'] and r['repaired'])
    avg_completeness = sum(r['completeness'] for r in results) / n if n else 0
    
    lines.append(f"\n{'=' * 60}")
    lines.append(f"SUMMARY: {n} curiosities, {complete} complete, {repaired} repaired")
    lines.append(f"Avg completeness: {avg_completeness:.2f}/4")
    lines.append(f"Repair rate: {repaired/n*100:.1f}%" if n else "")
    
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Curiosity Structural Validator and Repair")
    parser.add_argument('text', nargs='?', help="Curiosity text to validate")
    parser.add_argument('--check', action='store_true', help="Check only, don't repair")
    parser.add_argument('--batch-input', help="JSON file with list of curiosities")
    parser.add_argument('--json', action='store_true', help="Output as JSON")
    args = parser.parse_args()
    
    if args.batch_input:
        with open(args.batch_input) as f:
            curiosities = json.load(f)
        results = []
        for c in curiosities:
            # Accept either a bare string or a {"id":..., "text":...} object.
            if isinstance(c, dict):
                cid = c.get('id')
                ctext = c.get('text', '')
                r = validate_and_repair(ctext)
                if isinstance(r, dict):
                    r['id'] = cid
                results.append(r)
            else:
                results.append(validate_and_repair(c))
    elif args.text:
        results = [validate_and_repair(args.text)]
    else:
        print("Provide text or --batch-input")
        return
    
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(format_report(results))


if __name__ == '__main__':
    main()
