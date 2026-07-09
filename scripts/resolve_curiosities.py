from db_retry import get_db
#!/usr/bin/env python3
"""
resolve_curiosities.py — Fast curiosity resolution via keyword indexing.

Uses inverted index for O(1) candidate lookup instead of O(n*m) brute force.
Only checks curiosities against experiments created since last check.
"""
import sqlite3
import os
import re
import sys
import time
import json
from collections import defaultdict

DB_PATH = os.path.join(os.path.expanduser("~"), ".hermes", "prometheus.db")
JACCARD_THRESHOLD = 0.30
STOP_WORDS = {'the','a','an','is','are','was','were','be','been','being','have','has','had',
              'do','does','did','will','would','could','should','may','might','shall','can',
              'to','of','in','for','on','with','at','by','from','as','into','through','during',
              'before','after','above','below','between','out','off','over','under','again',
              'further','then','once','here','there','when','where','why','how','all','both',
              'each','few','more','most','other','some','such','no','nor','not','only','own',
              'same','so','than','too','very','just','because','but','and','or','if','while',
              'about','against','it','its','this','that','these','those','what','which','who',
              'whom','i','me','my','we','our','you','your','he','him','his','she','her','they',
              'them','their','what','which','who','whom','does','don','t','s','re','ve','ll',
              'd','m','ain','aren','couldn','didn','doesn','hadn','hasn','haven','isn','ma',
              'mightn','mustn','needn','shan','shouldn','wasn','weren','won','wouldn'}

def tokenize(text):
    if not text:
        return set()
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s]', ' ', text)
    return set(w for w in text.split() if w not in STOP_WORDS and len(w) > 2)

def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def main():
    conn = get_db(DB_PATH)
    c = conn.cursor()

    # Get last check time
    last_check_path = os.path.expanduser("~/.hermes/.curiosity_last_check")
    last_check = 0
    if os.path.exists(last_check_path):
        with open(last_check_path) as f:
            last_check = float(f.read().strip())

    # Get active curiosities (only those not checked in last 10 minutes)
    c.execute("""SELECT id, text, created_at, source_experiment, provenance FROM curiosities
                 WHERE status = 'active'
                 AND (created_at > ? OR ? = 0)""",
              (last_check - 600, last_check))
    curiosities = c.fetchall()
    if not curiosities:
        print("No active curiosities to check")
        conn.close()
        return

    # Get experiments created since last check (or all if first run)
    if last_check:
        c.execute("""SELECT id, hypothesis FROM experiments 
                     WHERE status = 'completed' 
                     AND hypothesis IS NOT NULL
                     AND created_at > ?""", (last_check - 3600,))
    else:
        c.execute("""SELECT id, hypothesis FROM experiments 
                     WHERE status = 'completed' 
                     AND hypothesis IS NOT NULL
                     ORDER BY created_at DESC LIMIT 3000""")
    experiments = c.fetchall()

    print(f"Checking {len(curiosities)} curiosities against {len(experiments)} experiments")

    # Build inverted index on experiments
    exp_index = defaultdict(list)  # keyword -> [(eid, token_set)]
    exp_all = []
    for eid, hyp in experiments:
        tokens = tokenize(hyp)
        exp_all.append((eid, tokens))
        for word in tokens:
            exp_index[word].append((eid, tokens))

    # Resolve
    resolved = 0
    for cur_id, cur_text, cur_created, cur_source, cur_prov in curiosities:
        # Parse JSON text
        if isinstance(cur_text, str) and cur_text.startswith('{'):
            try:
                cur_obj = json.loads(cur_text)
                cur_text = cur_obj.get('text', cur_text)
            except:
                pass
        cur_tokens = tokenize(cur_text)
        if not cur_tokens:
            continue

        # Fast candidate selection: experiments sharing most keywords
        candidate_scores = defaultdict(int)
        for word in cur_tokens:
            for eid, exp_toks in exp_index.get(word, []):
                candidate_scores[eid] += 1

        # Check top 20 candidates
        if not candidate_scores:
            continue
        top_candidates = sorted(candidate_scores.items(), key=lambda x: -x[1])[:20]

        best_match = None
        best_score = 0.0
        # Retest curiosities may not be resolved by text-match at all. They
        # exist to be answered by a NEW experiment dispatched for them, and
        # any Jaccard close is false: matching their own source experiment
        # (whose hypothesis their text quotes) self-resolved 151 of them, and
        # matching some other recent experiment is not a retest either. Every
        # close without a replication_results credit permanently locks the
        # claim out of the retest gate via the injector's
        # one-injection-per-claim dedup. Their real closer is intake block 1g
        # (apply_worker_results), which resolves them at the moment the
        # completed retest's credit row is written.
        if cur_prov in ('candidate_retest', 'retest_gate'):
            continue
        for eid, _ in top_candidates:
            # Find the token set for this eid
            exp_toks = None
            for e, t in exp_all:
                if e == eid:
                    exp_toks = t
                    break
            if exp_toks is None:
                continue
            score = jaccard(cur_tokens, exp_toks)
            if score > best_score:
                best_score = score
                best_match = eid

        if best_score >= JACCARD_THRESHOLD and best_match:
            c.execute(
                "UPDATE curiosities SET status = 'resolved', resolved_by_experiment = ?, resolved_at = ? WHERE id = ?",
                (best_match, time.time(), cur_id)
            )
            resolved += 1

    conn.commit()

    # Save last check time
    with open(last_check_path, 'w') as f:
        f.write(str(time.time()))

    # Report
    c.execute("SELECT COUNT(*) FROM curiosities WHERE status = 'active'")
    remaining = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM curiosities WHERE status = 'resolved'")
    total_resolved = c.fetchone()[0]

    print(f"Resolved: {resolved}")
    print(f"Remaining active: {remaining}")
    print(f"Total resolved: {total_resolved}")

    conn.close()

if __name__ == "__main__":
    main()
