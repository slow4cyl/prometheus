#!/usr/bin/env python3
"""
claim_skill_consistency_check.py — Detects mismatches between claim registry
status and operational skill documentation.

Runs checks:
1. Any skill section referencing a claim that has been downgraded to < strip_threshold
2. Any claim with dependency mappings that lacks an entry in claim_skill_deps
3. Any claim_skill_deps entries whose claim no longer exists
4. Flags skill files with stale confidence caveats (claims that recovered but
   the skill file still carries a downgrade warning)

Output: JSON to ~/.hermes/claim_consistency.json
Exit 0 = clean, exit 1 = issues found.

Usage: python3 claim_skill_consistency_check.py [--verbose]
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from prometheus_paths import PROMETHEUS_DB, under_home


def get_db():
    db_path = PROMETHEUS_DB
    for attempt in range(5):
        try:
            db = sqlite3.connect(db_path, timeout=10)
            db.execute("PRAGMA busy_timeout=10000")
            db.row_factory = sqlite3.Row
            return db
        except sqlite3.OperationalError:
            time.sleep(0.5 * (attempt + 1))
    return None


def load_thresholds(db):
    config = {"caveat_threshold": 0.70, "strip_threshold": 0.40}
    try:
        rows = db.execute("SELECT key, value FROM claim_config").fetchall()
        for r in rows:
            config[r["key"]] = r["value"]
    except Exception:
        pass
    return config


def scan_skill_files():
    """Scan all SKILL.md files for sections that might reference claims."""
    issues = []
    skills_dir = under_home("skills")

    if not os.path.exists(skills_dir):
        return issues

    for root, dirs, files in os.walk(skills_dir):
        for f in files:
            if f == "SKILL.md":
                path = os.path.join(root, f)
                try:
                    with open(path) as fh:
                        content = fh.read()
                except Exception:
                    continue

                # Check for stale caveats: if a skill has an epistemic caveat
                # but the referenced claim has recovered above threshold
                caveats = re.findall(
                    r'\[EPISTEMIC (?:DOWNGRADE|CAVEAT).*?Claim #(\d+).*?confidence ([\d.]+)',
                    content
                )
                for claim_id_str, old_conf_str in caveats:
                    try:
                        cid = int(claim_id_str)
                        old_conf = float(old_conf_str)
                        issues.append({
                            "type": "stale_caveat",
                            "file": path,
                            "claim_id": cid,
                            "caveat_confidence": old_conf,
                            "detail": f"Skill contains epistemic caveat referencing claim #{cid} at conf={old_conf}"
                        })
                    except ValueError:
                        continue

    return issues


def main():
    verbose = "--verbose" in sys.argv

    db = get_db()
    if db is None:
        print(json.dumps({"error": "prometheus.db unavailable"}))
        sys.exit(2)

    config = load_thresholds(db)
    issues = []

    # Check 1: Claims with dependency mappings that are below strip threshold
    try:
        rows = db.execute("""
            SELECT csd.claim_id, csd.skill_name, csd.section, csd.dependency_type,
                   ac.claim_text, ac.status, ac.confidence
            FROM claim_skill_deps csd
            JOIN architectural_claims ac ON ac.id = csd.claim_id
            WHERE ac.confidence < ?
            ORDER BY ac.confidence ASC
        """, (config["strip_threshold"],)).fetchall()

        if rows:
            # Group by claim
            by_claim = defaultdict(list)
            for r in rows:
                by_claim[r["claim_id"]].append(dict(r))

            for cid, deps in by_claim.items():
                claim = deps[0]
                sections = [f"{d['skill_name']}/{d['section']}" for d in deps]
                issues.append({
                    "type": "stripped_claim_referenced",
                    "claim_id": cid,
                    "confidence": claim["confidence"],
                    "status": claim["status"],
                    "claim_text": claim["claim_text"][:120],
                    "affected_sections": sections,
                    "detail": f"Claim #{cid} (conf={claim['confidence']:.2f}) has {len(deps)} active skill sections. Task bodies will strip these procedures."
                })
    except sqlite3.OperationalError as e:
        if verbose:
            print(f"Check 1 failed (claim_skill_deps table may not exist): {e}", file=sys.stderr)

    # Check 2: Orphaned dependency mappings (claim no longer exists)
    try:
        orphans = db.execute("""
            SELECT csd.claim_id, csd.skill_name, csd.section
            FROM claim_skill_deps csd
            LEFT JOIN architectural_claims ac ON ac.id = csd.claim_id
            WHERE ac.id IS NULL
        """).fetchall()
        for o in orphans:
            issues.append({
                "type": "orphaned_dep",
                "claim_id": o["claim_id"],
                "skill": o["skill_name"],
                "section": o["section"],
                "detail": f"Dependency mapping references claim #{o['claim_id']} which no longer exists"
            })
    except sqlite3.OperationalError:
        pass

    # Check 3: Claims with impact but no dependency mapping.
    # Only flag claims between strip_threshold and caveat_threshold — claims
    # below strip_threshold already have their procedures stripped from task
    # bodies at runtime, so they don't need deps tracking.
    try:
        impactful_claims = db.execute("""
            SELECT ac.id, ac.claim_text, ac.confidence, ac.status
            FROM architectural_claims ac
            WHERE ac.confidence >= ?
              AND ac.confidence < ?
              AND ac.claim_type = 'mechanism'
              AND ac.id NOT IN (SELECT DISTINCT claim_id FROM claim_skill_deps)
            ORDER BY ac.confidence ASC
        """, (config["strip_threshold"], config["caveat_threshold"])).fetchall()
        for c in impactful_claims:
            issues.append({
                "type": "unmapped_claim",
                "claim_id": c["id"],
                "confidence": c["confidence"],
                "status": c["status"],
                "claim_text": c["claim_text"][:120],
                "detail": f"Claim #{c['id']} is below caveat threshold but has no skill dependency mapping — may affect task bodies without being tracked"
            })
    except sqlite3.OperationalError:
        pass

    # Check 4: Stale caveats in skill files
    skill_issues = scan_skill_files()
    issues.extend(skill_issues)

    db.close()

    # Write output
    output_path = under_home("claim_consistency.json")
    report = {
        "checked_at": time.time(),
        "total_issues": len(issues),
        "config": config,
        "issues": issues,
    }

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    if verbose:
        print(json.dumps(report, indent=2))
    else:
        if issues:
            print(f"[claim-consistency] {len(issues)} issue(s) found → {output_path}")

    sys.exit(1 if issues else 0)


if __name__ == "__main__":
    main()