#!/usr/bin/env python3
"""Dump per-domain BENCH3 accuracy to ~/.hermes/domain_accuracy.json.
Used by external-probe to target low-accuracy domains."""
import json, os, sqlite3, sys, time

DB = os.path.expanduser("~/.hermes/prometheus.db")
OUT = os.path.expanduser("~/.hermes/domain_accuracy.json")

# Use db_retry if available, otherwise fall back to busy_timeout
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from db_retry import get_db
    conn = get_db(DB, readonly=True)
except ImportError:
    conn = sqlite3.connect(DB, timeout=30)
    for attempt in range(5):
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            break
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.5)

rows = conn.execute("""
    SELECT e.domain,
           COUNT(*) as total,
           SUM(CASE WHEN (c.known_answer='true' AND wr.hypothesis_supported=1)
                      OR (c.known_answer='false' AND wr.hypothesis_supported=0) THEN 1 ELSE 0 END) as correct
    FROM curiosities c
    JOIN experiments e ON c.source_experiment = e.id
    JOIN worker_results wr ON e.id = wr.experiment_id
    WHERE c.benchmark_id LIKE 'BENCH3-%'
      AND c.known_answer IN ('true','false')
      AND e.domain IS NOT NULL
    GROUP BY e.domain
    HAVING total >= 2
    ORDER BY CAST(correct AS REAL) / total ASC
""").fetchall()

data = {}
for domain, total, correct in rows:
    acc = correct / total if total > 0 else 0
    data[domain] = {"total": total, "correct": correct, "accuracy": round(acc, 3)}

conn.close()

with open(OUT, 'w') as f:
    json.dump(data, f, indent=2)

# Print summary for log
low = {d: v for d, v in data.items() if v["accuracy"] < 0.85}
print(f"Domain accuracy dumped: {len(data)} domains, {len(low)} below 85%")
for d, v in sorted(low.items(), key=lambda x: x[1]["accuracy"]):
    print(f"  {d}: {v['correct']}/{v['total']} = {v['accuracy']:.1%}")
