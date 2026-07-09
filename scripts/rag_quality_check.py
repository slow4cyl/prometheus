#!/usr/bin/env python3
"""RAG quality check — re-indexes incrementally then reports quality metrics."""
import sys, os
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
from experiment_rag import query_quality_stats, init_db

# NOTE: Indexing is handled separately by rag-index-guard (cron, every 2m)
# which uses the lighter _index_prometheus_experiments(only_missing=True) approach
# and avoids the "Too many open files" crash that index_experiments() triggers
# when ~91K embedding files exist with only 1024 fd limit. 
# If you need to manually re-index, run: python3 ~/.hermes/scripts/experiment_rag.py index

init_db()
stats = query_quality_stats(days=7)

issues = []
if stats["total_queries"] == 0:
    issues.append("No RAG queries in 7 days — workers may not be using the system")
elif stats["avg_score"] < 0.4:
    issues.append(f"Low average score ({stats['avg_score']:.3f}) — results may not be relevant")
elif stats["low_quality_pct"] > 20:
    issues.append(f"High low-quality query rate ({stats['low_quality_pct']:.0f}%) — indexing may need improvement")

if issues:
    print(f"RAG Quality Alert:")
    for issue in issues:
        print(f"  ⚠ {issue}")
    print(f"Stats: {stats['total_queries']} queries, avg score {stats['avg_score']:.3f}, "
          f"{stats['unique_workers']} unique workers")
    sys.exit(1)
else:
    sys.exit(0)
