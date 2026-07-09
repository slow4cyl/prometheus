#!/usr/bin/env python3
"""
Auto-Expand Verification Database
=================================
Extracts ground-truth claims from ALL completed experiments in self_state.json
and registers them in the verification pipeline's contradiction/support databases.

Also feeds the working memory daemon with experiment-derived concepts and
associations, keeping the concept graph growing with new findings.

Usage:
  python3 ~/.hermes/scripts/auto_expand_verification_db.py
  python3 ~/.hermes/scripts/auto_expand_verification_db.py --dry-run
"""

import fcntl
import json
import os
import re
import sys
import time
import urllib.request

"""Auto Expand Verification Db — main entry point.

Run: python3 auto_expand_verification_db.py
"""

HOME = os.path.expanduser("~")
SELF_STATE = os.path.join(HOME, ".hermes", "self_state.json")
FVP_PATH = os.path.join(HOME, ".hermes", "scripts", "fact_verification_pipeline.py")
TGVA_PATH = os.path.join(HOME, ".hermes", "scripts", "textgrad_verified_augmentation.py")
AUDIT_LOG = os.path.join(HOME, ".hermes", "self_audit.log")
DAEMON_URL = "http://localhost:19876"
WATERMARK_PATH = os.path.join(HOME, ".hermes", "wm_expand_watermark.json")
LOCK_PATH = AUDIT_LOG + ".lock"


def load_state_safe():
    """Load self_state.json with state_lock for safe concurrent reads."""
    sys.path.insert(0, os.path.join(HOME, ".hermes", "scripts"))
    try:
        from state_lock import load_state
        return load_state()
    except ImportError:
        # Fallback: raw read (no lock available)
        with open(SELF_STATE, "r") as f:
            return json.load(f)


def load_watermark():
    """Load the experiment count watermark (last processed count)."""
    if os.path.exists(WATERMARK_PATH):
        try:
            with open(WATERMARK_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_experiment_count": 0, "last_run": None}


def save_watermark(count):
    """Save the current experiment count as watermark."""
    with open(WATERMARK_PATH, "w") as f:
        json.dump({
            "last_experiment_count": count,
            "last_run": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, f)


def extract_claims_from_experiments(state):
    """Extract ground-truth claims from all completed experiments."""
    experiments = state.get("experiments", {}).get("completed", [])
    
    contradiction_claims = []
    support_claims = []
    
    for exp in experiments:
        eid = exp.get("id", "unknown")
        hypothesis = exp.get("hypothesis", exp.get("curiosity", ""))
        result = exp.get("result", "")
        findings = exp.get("findings", [])
        
        is_refuted = "REFUTED" in result
        is_confirmed = "CONFIRMED" in result or "CONFIRMED" in hypothesis
        
        parsed_findings = []
        for line in result.split("\\n"):
            line = line.strip()
            for fname in [f"F{i}" for i in range(1, 11)]:
                if line.startswith(f"{fname} [") or line.startswith(f"{fname}:") or line.startswith(f"{fname} "):
                    parsed_findings.append(line)
                    break
        
        if not parsed_findings:
            for f_text in findings:
                if isinstance(f_text, str):
                    parsed_findings.append(f_text)
        
        if is_refuted:
            _, _, _, h_clean = extract_shortener(hypothesis, 120)
            contradiction_claims.append({
                "experiment": eid,
                "hypothesis": h_clean,
                "level": "REFUTED — opposite is true"
            })
            for finding in parsed_findings[:3]:
                if finding and not any(excl in finding for excl in ["METHODOLOGY", "COST:", "Total cost"]):
                    support_claims.append({
                        "experiment": eid,
                        "claim": finding[:200],
                        "level": "CONFIRMED (learned from refuted hypothesis)"
                    })
        elif is_confirmed:
            _, _, _, h_clean = extract_shortener(hypothesis, 120)
            support_claims.append({
                "experiment": eid,
                "claim": h_clean,
                "level": "CONFIRMED"
            })
            for finding in parsed_findings[:3]:
                if finding and not any(excl in finding for excl in ["METHODOLOGY", "COST:", "Total cost"]):
                    support_claims.append({
                        "experiment": eid,
                        "claim": finding[:200],
                        "level": "CONFIRMED"
                    })
        else:
            for finding in parsed_findings[:2]:
                if finding:
                    lower = finding.lower()
                    if any(kw in lower for kw in ["refuted", "not", "cannot", "fails", "worse", "underperform", "backfire"]):
                        contradiction_claims.append({
                            "experiment": eid,
                            "claim": finding[:200],
                            "level": "REFUTED (implicit)"
                        })
                    elif any(kw in lower for kw in ["confirmed", "works", "helps", "improves", "outperforms", "wins", "best"]):
                        support_claims.append({
                            "experiment": eid,
                            "claim": finding[:200],
                            "level": "CONFIRMED (implicit)"
                        })
    
    return contradiction_claims, support_claims


def extract_shortener(text, maxlen=120):
    """Truncate text to maxlen, preserving sentence boundaries."""
    if not text: return ("", text, 0, text)
    text_str = str(text)
    clean = text_str.replace("[", "").replace("]", "").replace("\\", " ")
    if len(clean) <= maxlen:
        return (clean, clean, len(clean), clean)
    return (clean[:maxlen-3] + "...", clean, 0, clean)


def extract_domain(hypothesis):
    """Extract a rough domain keyword from hypothesis text."""
    h = hypothesis.lower()
    # Common domain keywords
    domains = {
        "protein": "protein_folding", "folding": "protein_folding",
        "enzyme": "enzyme_kinetics", "kinetic": "enzyme_kinetics",
        "quantum": "quantum_computing", "qubit": "quantum_computing",
        "neural": "neural_networks", "gradient": "neural_networks",
        "climate": "climate_science", "temperature": "climate_science",
        "ecology": "ecology", "species": "ecology",
        "finance": "finance", "market": "finance", "stock": "finance",
        "bridge": "bridge_dynamics", "resonance": "bridge_dynamics",
        "injection": "injection_detection", "adversarial": "injection_detection",
        "calibration": "calibration", "confidence": "calibration",
        "transfer": "cross_domain_transfer",
        "pruning": "model_pruning", "quantiz": "quantization",
        "nois": "noise_dynamics", "signal": "signal_processing",
        "seismic": "seismology", "earthquake": "seismology",
        "volcan": "volcanology", "magma": "volcanology",
        "epidemic": "epidemiology", "vaccine": "epidemiology",
        "material": "materials_science", "polymer": "materials_science",
        "acoustic": "acoustics", "sound": "acoustics",
        "optical": "optics", "photon": "optics",
    }
    for keyword, domain in domains.items():
        if keyword in h:
            return domain
    return "general"


def feed_concepts_to_daemon(contradiction_claims, support_claims):
    """Extract concepts from claims and feed them to the WM daemon via /add."""
    fed = 0
    errors = 0
    
    # Group claims by domain for association building
    domain_claims = {}
    all_claims = contradiction_claims + support_claims
    for claim in all_claims:
        hypothesis = claim.get("hypothesis", claim.get("claim", ""))
        domain = extract_domain(hypothesis)
        if domain not in domain_claims:
            domain_claims[domain] = []
        domain_claims[domain].append(claim)
    
    # Feed domain-level concepts with associations to related experiments
    for domain, claims in domain_claims.items():
        associations = []
        for claim in claims[:10]:  # Cap per domain to avoid flooding
            eid = claim.get("experiment", "")
            hypothesis = claim.get("hypothesis", claim.get("claim", ""))[:80]
            is_confirmed = "CONFIRMED" in claim.get("level", "")
            weight = 0.8 if is_confirmed else 0.5
            if eid and hypothesis:
                associations.append({"target": hypothesis, "weight": weight})
        
        if associations:
            try:
                data = json.dumps({
                    "concept": domain,
                    "associations": associations,
                }).encode()
                req = urllib.request.Request(
                    f"{DAEMON_URL}/add", data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
                fed += 1
            except Exception as e:
                errors += 1
    
    # Feed experiment-level concepts with cross-domain associations
    for claim in all_claims[:30]:  # Cap total to keep runs fast
        eid = claim.get("experiment", "")
        hypothesis = claim.get("hypothesis", claim.get("claim", ""))[:80]
        if not eid or not hypothesis:
            continue
        
        # Find related claims in other domains
        domain = extract_domain(hypothesis)
        associations = []
        for other in all_claims[:30]:
            other_h = other.get("hypothesis", other.get("claim", ""))[:80]
            other_eid = other.get("experiment", "")
            other_domain = extract_domain(other_h)
            if other_eid == eid or not other_h:
                continue
            # Cross-domain associations get higher weight
            if other_domain != domain:
                associations.append({"target": other_h, "weight": 0.7})
            else:
                associations.append({"target": other_h, "weight": 0.5})
            if len(associations) >= 5:
                break
        
        if associations:
            try:
                data = json.dumps({
                    "concept": hypothesis,
                    "associations": associations,
                }).encode()
                req = urllib.request.Request(
                    f"{DAEMON_URL}/add", data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
                fed += 1
            except Exception as e:
                errors += 1
    
    return fed, errors


def log_audit(contradiction_count, support_count, concepts_fed=0):
    """Append to audit log with file locking."""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = f"[{timestamp}] AUTO-EXPAND CYCLE\n"
    entry += f"  Extracted {contradiction_count} contradiction entries and {support_count} support entries\n"
    entry += f"  Fed {concepts_fed} concepts to working memory daemon\n"
    
    # File-locked append
    try:
        fd = open(LOCK_PATH, "a+")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with open(AUDIT_LOG, "a") as f:
                f.write(entry)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
    except Exception:
        # Best effort — don't crash on audit log failure
        with open(AUDIT_LOG, "a") as f:
            f.write(entry)


def update_daemon(focus, associations=None):
    """Update working memory daemon focus."""
    try:
        data = json.dumps({"concept": focus}).encode()
        req = urllib.request.Request(
            DAEMON_URL + "/focus", data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # Non-fatal


def main():
    dry_run = "--dry-run" in sys.argv
    
    print("Loading self_state.json...")
    state = load_state_safe()
    experiments = state.get("experiments", {}).get("completed", [])
    total_experiments = len(experiments)
    print(f"Found {total_experiments} completed experiments")
    
    # Check watermark — skip if no new experiments
    watermark = load_watermark()
    last_count = watermark.get("last_experiment_count", 0)
    if total_experiments <= last_count and not dry_run:
        print(f"No new experiments since last run ({last_count}). Skipping.")
        return 0
    
    new_count = total_experiments - last_count
    print(f"New experiments since last run: {new_count}")
    
    print("\nExtracting ground-truth claims...")
    contradiction_claims, support_claims = extract_claims_from_experiments(state)
    
    print(f"  Contradiction entries: {len(contradiction_claims)}")
    print(f"  Support entries: {len(support_claims)}")
    
    if not dry_run:
        # Feed concepts to working memory daemon
        print("\nFeeding concepts to working memory daemon...")
        fed, errors = feed_concepts_to_daemon(contradiction_claims, support_claims)
        print(f"  Concepts fed: {fed}, errors: {errors}")
        
        # Update focus
        update_daemon("auto_expand_verification_db")
        
        # Log audit
        log_audit(len(contradiction_claims), len(support_claims), fed)
        
        # Update watermark
        save_watermark(total_experiments)
        
        print(f"\nDone. {fed} concepts fed, watermark updated to {total_experiments}.")
    else:
        print("\n[DRY RUN — no changes made]")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
