from db_retry import get_db
#!/usr/bin/env python3
"""
Domain Taxonomy Maintenance — runs merge + confidence update.
Designed to be called periodically (cron) to prevent domain fragmentation.

Steps:
  1. Run domain_taxonomy_merge.py to consolidate known variants
  2. Run update_domain_confidence.py to refresh confidence scores
  3. Report any new unmapped domains that may need canonical mappings

Exit codes:
  0 = healthy (no issues)
  1 = warnings (new unmapped domains detected)
  2 = error (script failure)
"""

import os
import sys
import subprocess
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

def run_script(name, args=None):
    """Run a script and return (exit_code, stdout, stderr)."""
    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, name)]
    if args:
        cmd.extend(args)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return result.returncode, result.stdout, result.stderr

def main():
    print("=" * 60)
    print("DOMAIN TAXONOMY MAINTENANCE")
    print("=" * 60)
    
    # Step 1: Run merge
    print("\n[1/5] Running domain taxonomy merge...")
    rc, stdout, stderr = run_script("domain_taxonomy_merge.py")
    if rc != 0:
        print(f"  ERROR: merge failed (exit {rc})")
        print(stderr)
        return 2
    # Extract key info from merge output
    for line in stdout.split("\n"):
        if "Merged" in line or "Final unique" in line or "Domains that would be merged" in line:
            print(f"  {line.strip()}")
    
    # Step 2: Normalize worker_results domains to canonical form
    print("\n[2/5] Normalizing worker_results domains...")
    rc, stdout, stderr = run_script("normalize_all_domains.py")
    if rc != 0:
        print(f"  WARN: normalize failed (exit {rc})")
        print(stderr[:200] if stderr else "")
    else:
        for line in stdout.split("\n"):
            if "updated" in line.lower() or "canonical" in line.lower() or "rebuilt" in line.lower():
                print(f"  {line.strip()}")
    
    # Step 3: Auto-classify uncategorized experiments
    print("\n[3/5] Auto-classifying uncategorized experiments...")
    rc, stdout, stderr = run_script("auto_classify_uncategorized.py")
    if rc != 0:
        print(f"  WARN: auto-classify failed (exit {rc})")
    else:
        for line in stdout.split("\n"):
            if "Updated" in line or "No match" in line or "Found" in line:
                print(f"  {line.strip()}")
    
    # Step 4: Update confidence
    print("\n[4/5] Updating domain confidence scores...")
    rc, stdout, stderr = run_script("update_domain_confidence.py")
    if rc != 0:
        print(f"  ERROR: confidence update failed (exit {rc})")
        print(stderr)
        return 2
    # Count updates
    update_count = stdout.count("->")
    print(f"  Updated {update_count} domain confidence scores")
    
    # Step 5: Prune empty domains from domains table
    print("\n[5/6] Pruning empty domains from domains table...")
    conn = get_db()
    cur = conn.execute("""
        DELETE FROM domains WHERE name NOT IN (
            SELECT DISTINCT domain FROM experiments WHERE domain IS NOT NULL
        )
    """)
    pruned = cur.rowcount
    conn.commit()
    conn.close()
    if pruned:
        print(f"  Pruned {pruned} empty domains")
    else:
        print("  No empty domains to prune")

    # Step 6: Check for new unmapped domains
    print("\n[6/6] Checking for new unmapped domains...")
    rc, stdout, stderr = run_script("domain_taxonomy_merge.py", ["--dry-run"])
    if rc == 0:
        for line in stdout.split("\n"):
            if "Unmapped domains" in line:
                print(f"  {line.strip()}")
            elif "Domains that would be merged" in line:
                print(f"  {line.strip()}")
    
    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    return 0

if __name__ == "__main__":
    sys.exit(main())
