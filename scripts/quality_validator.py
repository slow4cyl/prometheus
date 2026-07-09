#!/usr/bin/env python3
"""
quality_validator.py — Validate experiment quality before writing to experiments table.

Called by apply_worker_results.py for each worker result. Returns a quality
score (0-100), tags, and a reject flag. Results scoring below 40 are rejected
and must NOT be written to the experiments table. This prevents zombie
experiments with empty/meaningless results from polluting the dashboard.

Quality dimensions:
  1. Finding length (minimum substantive content)
  2. Substantive content (mechanism, measurement, comparison)
  3. Not a self-identified duplicate
  4. Hypothesis-result alignment (not just verdict copied)
  5. Confidence reasonableness

Usage:
    from quality_validator import validate_quality
    score, tags = validate_quality(hypothesis, key_finding, confidence, exp_id)
    if score < 40:
        tags.append("LOW_QUALITY")
"""
import re

# Minimum key_finding length for a substantive result

"""Quality Validator.

Part of the Prometheus research infrastructure.
"""

MIN_FINDING_LENGTH = 80

# Patterns that indicate low-quality or problematic results
JUNK_PATTERNS = [
    r"^exp_\d+\s+complete",
    r"^JANITOR:",
    r"^ARCHIVED:",
    r"^cleaned?\s",
    r"^reclaimed?\s",
    r"^blocked\s",
    r"^no\s+(change|result|data|effect|difference|finding)",
    r"^inconclusive",
    r"^could not",
    r"^failed to",
    r"^error:",
    r"^timeout",
    r"^skipped",
]

# Patterns that indicate the finding is actually a duplicate/already answered
DUPLICATE_PATTERNS = [
    r"ALREADY\s+ANSWERED",
    r"ALREADY\s+THOROUGHLY",
    r"ALREADY\s+EXHAUSTIVELY",
    r"DUPLICATE\s+of",
    r"DUPLICATE:",
    r"already\s+resolved",
    r"already\s+covered",
]

# Patterns that indicate substantive content
SUBSTANTIVE_PATTERNS = [
    r"F1[=\s>]",           # F1 score mentioned
    r"accuracy[=\s>]",     # Accuracy mentioned
    r"precision[=\s>]",    # Precision mentioned
    r"recall[=\s>]",       # Recall mentioned
    r"AUC[=\s>]",          # AUC mentioned
    r"p[\s=<]",            # p-value mentioned
    r"improve[sd]?\s",     # Improvement mentioned
    r"reduce[sd]?\s",      # Reduction mentioned
    r"better\s+than",      # Comparison
    r"worse\s+than",       # Comparison
    r"outperform",         # Comparison
    r"mechanism",          # Mechanism explanation
    r"because",            # Causal reasoning
    r"due\s+to",           # Causal reasoning
    r"CONFIRMED",          # Clear verdict
    r"REFUTED",            # Clear verdict
    r"SUPPORTED",          # Clear verdict
    r"universal",          # Generalization claim
    r"transfer",           # Transfer claim
    r"generaliz",          # Generalization claim
]

# Blocker caveats (2026-07-01): phrases where the worker ITSELF admits the
# result was not independently verified. When present, the reported confidence
# must not exceed CAVEAT_CONFIDENCE_CAP — otherwise honest caveats become
# decoration while a 0.9+ confidence propagates (e.g. exp_ice_xxi_verification:
# "Cannot independently replicate XFEL experiments computationally ...
# Confidence: 0.92"). IMPORTANT: these do NOT reduce the quality score.
# Penalizing the caveat would teach workers to omit it; only the confidence
# is capped, at intake (apply_worker_results) and at write time
# (write_worker_result).
BLOCKER_CAVEAT_PATTERNS = [
    r"cannot\s+(?:be\s+)?independently\s+(?:replicat|verif|validat|confirm|reproduc|test)",
    r"(?:could\s+not|couldn'?t|unable\s+to)\s+independently\s+(?:replicat|verif|validat|confirm|reproduc|test)",
    r"no\s+independent\s+(?:replication|verification|validation|confirmation|test(?:ing)?)",
    r"not\s+(?:been\s+)?independently\s+(?:replicated|verified|validated|confirmed|tested|reproduced)",
    r"without\s+independent\s+(?:replication|verification|validation|confirmation)",
    r"cannot\s+(?:replicate|verify|validate|confirm|reproduce)\s+[^.;\n]{0,60}(?:computationally|experimentally|independently)",
    r"relies\s+(?:solely|entirely|only|primarily)\s+on\s+(?:the\s+)?(?:paper|publication|published|authors?|literature|reported\s+(?:values|results))",
    r"based\s+(?:solely|entirely|only)\s+on\s+(?:the\s+)?(?:paper|publication|literature|authors)",
    r"no\s+(?:direct\s+)?experimental\s+(?:verification|validation|data)",
]

CAVEAT_CONFIDENCE_CAP = 0.6


def detect_blocker_caveats(text):
    """Return the list of blocker-caveat phrases matched in text (may be empty)."""
    if not text:
        return []
    matched = []
    for pattern in BLOCKER_CAVEAT_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            matched.append(m.group(0)[:80])
    return matched


def caveat_confidence_cap(key_finding, confidence):
    """Cap confidence at CAVEAT_CONFIDENCE_CAP when the finding itself says the
    result was not independently verified.

    Returns (capped_confidence, matched_caveats). confidence passes through
    unchanged (and matched list may still be non-empty) when it is already
    at or below the cap, non-numeric, or no caveat is present.
    """
    matched = detect_blocker_caveats(key_finding)
    try:
        conf_val = float(confidence) if confidence is not None else None
    except (ValueError, TypeError):
        conf_val = None
    if matched and conf_val is not None and conf_val > CAVEAT_CONFIDENCE_CAP:
        return CAVEAT_CONFIDENCE_CAP, matched
    return confidence, matched


# Patterns that indicate EXPLICIT mechanistic explanation (exp_72526493 fix)
# These get a much larger bonus because mechanism clarity is the #1
# differentiator for citation potential (delta=+7.3 between top/bottom 10%)
MECHANISM_PATTERNS = [
    r"WHY IT WORKS:",      # Explicit mechanism section
    r"MECHANISM:",         # Explicit mechanism section
    r"WHY:\s",             # Causal explanation
    r"because\s+\w+\s+(causes|produces|enables|creates|allows|prevents|forces|drives|results\s+in|leads\s+to)",
    r"the\s+mechanism\s+is",
    r"this\s+(works|happens|occurs|fails)\s+because",
    r"due\s+to\s+the\s+fact",
    r"causal\s+(mechanism|chain|pathway)",
]


def validate_quality(hypothesis, key_finding, confidence, exp_id=""):
    """
    Validate the quality of an experiment result.

    Returns (score: int 0-100, tags: list[str], reject: bool)
    """
    score = 100
    tags = []

    finding = (key_finding or "").strip()
    hyp = (hypothesis or "").strip()

    # 1. Length check
    if len(finding) < MIN_FINDING_LENGTH:
        score -= 40
        tags.append("SHORT_FINDING")

    if len(finding) < 30:
        score -= 20
        tags.append("MINIMAL_FINDING")

    # 2. Junk pattern check
    for pattern in JUNK_PATTERNS:
        if re.search(pattern, finding, re.IGNORECASE):
            score -= 30
            tags.append("JUNK_RESULT")
            break

    # 3. Duplicate check
    for pattern in DUPLICATE_PATTERNS:
        if re.search(pattern, finding, re.IGNORECASE):
            score -= 50
            tags.append("SELF_DUPLICATE")
            break
        if re.search(pattern, hyp, re.IGNORECASE):
            score -= 50
            tags.append("SELF_DUPLICATE")
            break

    # 4. Empty or near-empty
    if not finding or len(finding) < 10:
        score -= 60
        tags.append("EMPTY_RESULT")

    if not hyp or len(hyp) < 10:
        score -= 30
        tags.append("EMPTY_HYPOTHESIS")

    # 5. Hypothesis-result duplication (worker copied verdict into both fields)
    if hyp and finding:
        hyp_clean = re.sub(r'^(HYPOTHESIS:|QUESTION:)\s*', '', hyp, flags=re.IGNORECASE).strip()
        if hyp_clean.lower() == finding.lower():
            score -= 40
            tags.append("HYPO_EQUALS_RESULT")
        # Check if hypothesis starts with verdict (worker wrote result into hypothesis)
        elif re.match(r'^(SUPPORTED|REFUTED|CONFIRMED|PARTIALLY)', hyp, re.IGNORECASE):
            score -= 20
            tags.append("HYPO_HAS_VERDICT")

    # 6. Substantive content bonus
    has_substantive = False
    for pattern in SUBSTANTIVE_PATTERNS:
        if re.search(pattern, finding, re.IGNORECASE):
            has_substantive = True
            break
    if has_substantive:
        score = min(100, score + 10)
    elif len(finding) > MIN_FINDING_LENGTH:
        # Long finding but no measurable content — slight penalty
        score -= 10
        tags.append("NO_MEASURABLE_CONTENT")

    # 6b. Mechanism clarity bonus (exp_72526493 fix)
    # Mechanism clarity is the #1 differentiator for citation potential
    # Top 10% score 9.5/20 on mechanism vs 2.2/10 for bottom 10%
    has_mechanism = False
    for pattern in MECHANISM_PATTERNS:
        if re.search(pattern, finding, re.IGNORECASE):
            has_mechanism = True
            break
    if has_mechanism:
        # Reduced 25 -> 10 (2026-07-01): a literal-string bonus this large is
        # a Goodhart target — writing "MECHANISM:" rescued junk findings
        # (score 30 -> 55, past the reject line) regardless of content. The
        # tag stays for citation analytics; verdict grounding now lives in
        # the structured verdict_basis field, not prose markers.
        score = min(100, score + 10)
        tags.append("HAS_MECHANISM")
    elif has_substantive:
        # Has metrics but no mechanism — this is the F1 trap
        # "High accuracy without mechanism explanation is not citable"
        tags.append("NO_MECHANISM")

    # 7. Confidence check
    try:
        conf_val = float(confidence) if confidence is not None else None
    except (ValueError, TypeError):
        conf_val = None
    if conf_val is not None:
        if conf_val < 0.3:
            score -= 15
            tags.append("VERY_LOW_CONFIDENCE")
        elif conf_val > 0.95 and not has_substantive:
            # Very high confidence without measurable content — suspicious
            score -= 10
            tags.append("HIGH_CONF_LOW_EVIDENCE")

    # 8. Blocker caveats — tag ONLY, never a score penalty (see
    # BLOCKER_CAVEAT_PATTERNS above: penalizing honesty teaches workers to
    # omit caveats). Confidence capping happens at the call sites.
    if detect_blocker_caveats(finding):
        tags.append("CAVEAT_UNVERIFIED")

    # Clamp score
    score = max(0, min(100, score))

    # Add quality tier tag
    if score >= 80:
        tags.append("QUALITY_HIGH")
    elif score >= 60:
        tags.append("QUALITY_MEDIUM")
    elif score >= 40:
        tags.append("QUALITY_LOW")
    else:
        tags.append("QUALITY_REJECT")

    reject = score < 40

    return score, tags, reject


def quality_summary(results):
    """Generate a quality summary for a batch of results.

    results: list of (exp_id, score, tags) tuples
    """
    if not results:
        return "No results to summarize"

    scores = [r[1] for r in results]
    avg = sum(scores) / len(scores)
    high = sum(1 for s in scores if s >= 80)
    medium = sum(1 for s in scores if 60 <= s < 80)
    low = sum(1 for s in scores if 40 <= s < 60)
    reject = sum(1 for s in scores if s < 40)

    # Tag frequency
    tag_counts = {}
    for _, _, tags in results:
        for t in tags:
            if t.startswith("QUALITY_"):
                continue
            tag_counts[t] = tag_counts.get(t, 0) + 1

    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:5]

    lines = [
        f"Quality: avg={avg:.0f} high={high} med={medium} low={low} reject={reject} (n={len(results)})",
    ]
    if top_tags:
        lines.append(f"Top issues: {', '.join(f'{t}({c})' for t, c in top_tags)}")

    return "\n".join(lines)


if __name__ == "__main__":
    # Quick test
    test_cases = [
        ("Does X work?", "CONFIRMED: X works with F1=0.95, accuracy=0.92, due to mechanism Y", 0.9, "exp_001"),
        ("Does Y work?", "exp_184 complete", 0.7, "exp_002"),
        ("Does Z work?", "ALREADY ANSWERED by exp_100", 0.85, "exp_003"),
        ("Does W work?", "", 0.5, "exp_004"),
        ("REFUTED: Does V work?", "REFUTED: REFUTED: Does V work?", 0.8, "exp_005"),
    ]

    for hyp, finding, conf, exp_id in test_cases:
        score, tags, reject = validate_quality(hyp, finding, conf, exp_id)
        print(f"{exp_id}: score={score} reject={reject} tags={tags}")
        print(f"  hyp={hyp[:50]}")
        print(f"  finding={finding[:60]}")
        print()
