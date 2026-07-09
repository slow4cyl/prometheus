#!/usr/bin/env python3
"""hf_cache_janitor.py — keep ~/.cache/huggingface under a size budget by
evicting least-recently-used models/datasets.

Why (2026-07-03): workers download models on demand for experiments; the cache
grew +68 GB in a single day and repeatedly filled the drive (95% twice in two
days). Staleness-based one-shot pruning didn't hold — the working set churns
too fast. This janitor enforces a hard budget instead: when the cache exceeds
BUDGET_GB, it deletes the least-recently-*accessed* model/dataset dirs until
under budget. Eviction is always safe: anything a future experiment needs is
re-downloaded automatically (cost = bandwidth + a few minutes, not
correctness).

LRU key = newest file atime under each model dir (a single touched file marks
the whole model hot). Silent when under budget (cron convention).

Usage:
    python3 hf_cache_janitor.py [--dry-run] [--budget-gb N]
"""
import argparse
import os
import shutil
import stat
import sys

CACHE = os.path.expanduser("~/.cache/huggingface")
ROOTS = [os.path.join(CACHE, "hub"), os.path.join(CACHE, "datasets")]
BUDGET_GB = 100


def dir_stats(path):
    """(total_bytes, newest_atime) for a tree.

    lstat + skip symlinks: HF hub snapshots/ symlink into blobs/ — following
    them double-counts every blob (os.stat resolves the link), which measured
    a 304 GiB cache as 555 GiB and would have over-evicted by ~2x.
    """
    total, newest = 0, 0.0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            total += st.st_size
            newest = max(newest, st.st_atime)
    return total, newest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget-gb", type=float, default=BUDGET_GB)
    args = ap.parse_args()
    budget = args.budget_gb * 2**30

    entries = []  # (newest_atime, size, path)
    total = 0
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            size, newest = dir_stats(p)
            entries.append((newest, size, p))
            total += size

    if total <= budget:
        return 0  # silent when healthy

    entries.sort()  # coldest first
    freed = 0
    print(f"HF cache {total/2**30:.1f} GiB > budget {budget/2**30:.0f} GiB — evicting coldest:")
    for newest, size, p in entries:
        if total - freed <= budget:
            break
        print(f"  {'[DRY-RUN] ' if args.dry_run else ''}evict {size/2**30:5.1f}G  {os.path.basename(p)}")
        if not args.dry_run:
            shutil.rmtree(p, ignore_errors=True)
        freed += size
    print(f"{'would free' if args.dry_run else 'freed'} {freed/2**30:.1f} GiB "
          f"-> cache {(total-freed)/2**30:.1f} GiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
