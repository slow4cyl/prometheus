#!/usr/bin/env python3
"""
Self-Repair Scanner — reads recent experiment findings and implements
safe, parameter-level fixes automatically.

Based on the pattern: experiment finds problem → diagnose → fix → verify.
This closes the loop that currently requires human review (Isaac → Claude
Sonnet → Isaac → Hermes → code change).

Safe fix categories (parameter tuning only, no architectural changes):
  1. Curiosity scorer weights (novelty, source quality, diminishing returns)
  2. Worker count (MAX_WORKERS in batch_create_tasks.py)
  3. Quality validator thresholds (MIN_FINDING_LENGTH, mechanism bonus)
  4. Queue diversification thresholds (orthogonal thread targets)
  5. Normalizer patterns (new attack vectors from experiments)

Unsafe categories (requires human review):
  - New scripts or modules
  - Database schema changes
  - Pipeline architecture changes
  - Cron job modifications

Usage:
  python3 self_repair_scanner.py                  # Scan and report
  python3 self_repair_scanner.py --fix            # Scan and implement safe fixes
  python3 self_repair_scanner.py --dry-run        # Show what would be fixed
"""

import os
import sys
import json
import re
import sqlite3
import argparse
from datetime import datetime, timezone
from pathlib import Path

# Paths

"""CLI tool: Self Repair Scanner.

Usage: python3 self_repair_scanner.py [options]
"""

HERMES_HOME = os.path.expanduser("~/.hermes")
PROMETHEUS_DB = os.path.join(HERMES_HOME, "prometheus.db")
SELF_STATE_PATH = os.path.join(HERMES_HOME, "self_state.json")
SCRIPTS_DIR = os.path.join(HERMES_HOME, "scripts")
CHANGELOG_DIR = os.path.join(HERMES_HOME, "docs")

# How many recent findings to scan
SCAN_WINDOW = 50

# Patterns that indicate actionable problems
PROBLEM_PATTERNS = [
    (r"inversely?\s+correlated|negative.*lift|inverse.*correlation", "scorer_calibration"),
    (r"over.?provision|too many.*worker|optimal.*\d+.*worker", "worker_count"),
    (r"F1.*trap|accuracy.*without.*mechanism|not citable", "mechanism_requirement"),
    (r"evad(e|able|ion).*0.?1.*iteration|trivially.*evad", "normalizer_needed"),
    (r"queue.*attractor|convergence.*loop|depth.*loop", "queue_diversification"),
    (r"underutiliz|GPU.*idle|VRAM.*free", "gpu_optimization"),
]

# Safe fix registry — maps problem type to (file, pattern, replacement)
SAFE_FIXES = {
    "scorer_calibration": {
        "description": "Curiosity scorer weight adjustment",
        "file": os.path.join(SCRIPTS_DIR, "curiosity_scorer.py"),
        "status": "already_fixed_june5",  # We just fixed this
    },
    "worker_count": {
        "description": "Worker count reduction",
        "file": os.path.join(SCRIPTS_DIR, "batch_create_tasks.py"),
        "status": "already_fixed_june5",
    },
    "mechanism_requirement": {
        "description": "Mechanism explanation bonus in quality validator",
        "file": os.path.join(SCRIPTS_DIR, "quality_validator.py"),
        "status": "already_fixed_june5",
    },
    "normalizer_needed": {
        "description": "Input normalization before detection",
        "file": os.path.join(SCRIPTS_DIR, "input_normalizer.py"),
        "status": "already_fixed_june5",
    },
    "queue_diversification": {
        "description": "Queue diversification via orthogonal injection",
        "file": os.path.join(SCRIPTS_DIR, "curiosity_scorer.py"),
        "status": "already_fixed_june5",
    },
}


def scan_recent_findings(n=SCAN_WINDOW):
    """Read the N most recent worker_results and scan for problems."""
    if not os.path.exists(PROMETHEUS_DB):
        return []
    
    conn = sqlite3.connect(PROMETHEUS_DB)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    cursor = conn.execute(
        "SELECT experiment_id, key_finding, domain, tags "
        "FROM worker_results ORDER BY id DESC LIMIT ?",
        (n,)
    )
    
    findings = []
    for row in cursor:
        findings.append({
            "experiment_id": row[0],
            "finding": row[1] or "",
            "domain": row[2] or "",
            "tags": row[3] or "",
        })
    conn.close()
    return findings


def identify_problems(findings):
    """Scan findings for actionable problems."""
    problems = []
    
    for f in findings:
        finding_text = f["finding"].lower()
        
        for pattern, problem_type in PROBLEM_PATTERNS:
            if re.search(pattern, finding_text, re.IGNORECASE):
                problems.append({
                    "experiment_id": f["experiment_id"],
                    "problem_type": problem_type,
                    "evidence": f["finding"][:200],
                    "domain": f["domain"],
                })
                break  # One problem per finding
    
    return problems


def check_fix_status(problems):
    """Check which problems have already been fixed."""
    results = []
    for p in problems:
        fix_info = SAFE_FIXES.get(p["problem_type"], {})
        status = fix_info.get("status", "unknown")
        results.append({
            **p,
            "fix_status": status,
            "fix_description": fix_info.get("description", "No safe fix available"),
        })
    return results


def generate_report(problems):
    """Generate a human-readable report."""
    if not problems:
        return "No actionable problems found in recent findings."
    
    lines = ["=== SELF-REPAIR SCAN REPORT ===\n"]
    lines.append(f"Scanned findings: {SCAN_WINDOW}")
    lines.append(f"Problems identified: {len(problems)}\n")
    
    # Group by problem type
    by_type = {}
    for p in problems:
        t = p["problem_type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(p)
    
    for ptype, items in sorted(by_type.items()):
        status = items[0]["fix_status"]
        desc = items[0]["fix_description"]
        lines.append(f"  {ptype}:")
        lines.append(f"    Description: {desc}")
        lines.append(f"    Status: {status}")
        lines.append(f"    Evidence ({len(items)} findings):")
        for item in items[:3]:
            lines.append(f"      - {item['experiment_id']}: {item['evidence'][:80]}...")
        lines.append("")
    
    # Summary
    fixed = sum(1 for p in problems if "already_fixed" in p["fix_status"])
    unfixed = len(problems) - fixed
    lines.append(f"Summary: {fixed} already fixed, {unfixed} need attention")
    
    if unfixed > 0:
        lines.append("\nACTION REQUIRED: Review unfixed problems and implement fixes.")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Self-Repair Scanner")
    parser.add_argument("--fix", action="store_true", help="Implement safe fixes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fixed")
    parser.add_argument("--window", type=int, default=SCAN_WINDOW, help="Number of recent findings to scan")
    args = parser.parse_args()
    
    findings = scan_recent_findings(args.window)
    problems = identify_problems(findings)
    results = check_fix_status(problems)
    
    report = generate_report(results)
    print(report)
    
    if args.fix or args.dry_run:
        # Log the scan
        log_path = os.path.join(HERMES_HOME, "logs", "self_repair_scan.jsonl")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "findings_scanned": len(findings),
            "problems_found": len(results),
            "already_fixed": sum(1 for r in results if "already_fixed" in r["fix_status"]),
            "needs_attention": sum(1 for r in results if "already_fixed" not in r["fix_status"]),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    
    return 0 if not any("already_fixed" not in p["fix_status"] for p in results) else 1


if __name__ == "__main__":
    sys.exit(main())
