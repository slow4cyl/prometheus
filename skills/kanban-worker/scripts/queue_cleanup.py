#!/usr/bin/env python3
"""
Queue Cleanup — one-time purge of resolved, duplicate, and excess queue items.

Removes:
  1. Items tagged [RESOLVED] or [PARTIALLY RESOLVED]
  2. Near-duplicate pairs (word overlap >0.5) — keeps the more specific one
  3. Excess items beyond 50 — drops lowest-scored

Writes cleaned queue back to self_state.json.

Usage:
    python3 queue_cleanup.py              # Preview what would be removed
    python3 queue_cleanup.py --apply      # Actually write changes
    python3 queue_cleanup.py --dry-run    # Same as preview (default)
"""

import json
import os
import re
import sys
from collections import Counter

SELF_STATE_PATH = os.path.expanduser("~/.hermes/self_state.json")
MAX_QUEUE_SIZE = 50
DEDUP_THRESHOLD = 0.5


def load_state():
    with open(SELF_STATE_PATH) as f:
        return json.load(f)


def save_state(state):
    with open(SELF_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_item_text(item):
    if isinstance(item, dict):
        return item.get("text", str(item))
    return str(item)


def is_resolved(text):
    tl = text.lower()
    return "[resolved" in tl or "[partially resolved" in tl or "[answered" in tl


def word_overlap(t1, t2):
    w1 = set(re.findall(r"\w{4,}", t1.lower()))
    w2 = set(re.findall(r"\w{4,}", t2.lower()))
    if not w1 or not w2:
        return 0
    return len(w1 & w2) / max(len(w1 | w2), 1)


def classify_thread(text):
    tl = text.lower()
    keywords = {
        "tfidf": ["tf-idf", "tfidf"],
        "injection": ["injection", "inject"],
        "calibration": ["calibration", "isotonic", "platt"],
        "attack": ["attack", "adversarial", "perturbation"],
        "embedding": ["embedding", "vector"],
        "domain": ["domain", "cross-domain"],
        "transfer": ["transfer", "few-shot"],
        "ensemble": ["ensemble", "xgboost", "random forest"],
    }
    for thread, kws in keywords.items():
        if any(kw in tl for kw in kws):
            return thread
    return "other"


def cleanup_queue(state, dry_run=True):
    queue = state.get("curiosity_queue", [])
    original_size = len(queue)

    # Step 1: Remove resolved items
    resolved = []
    remaining = []
    for item in queue:
        text = get_item_text(item)
        if is_resolved(text):
            resolved.append(item)
        else:
            remaining.append(item)

    # Step 2: Deduplicate — for pairs with >0.5 overlap, keep shorter (more specific)
    deduped = []
    removed_dupes = []
    seen = set()
    for i, item in enumerate(remaining):
        if i in seen:
            continue
        text_i = get_item_text(item)
        for j in range(i + 1, len(remaining)):
            if j in seen:
                continue
            text_j = get_item_text(remaining[j])
            if word_overlap(text_i, text_j) > DEDUP_THRESHOLD:
                seen.add(j)
                removed_dupes.append(remaining[j])
        deduped.append(item)

    # Step 3: Score and cap at MAX_QUEUE_SIZE
    # Simple scoring: use curiosity_scorer if available, else length-based
    try:
        sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
        from curiosity_scorer import score_all, load_state as scorer_load_state
        scorer_state = scorer_load_state()
        # Patch the queue temporarily for scoring
        orig_queue = scorer_state.get("curiosity_queue", [])
        scorer_state["curiosity_queue"] = deduped
        scored = score_all(scorer_state)
        scorer_state["curiosity_queue"] = orig_queue  # restore

        # Separate active (score > 0) from resolved/duplicate (score = 0)
        active = [s for s in scored if s["total"] > 0 and not s.get("resolved")]
        capped = []
        excess = []
        for item in deduped:
            text = get_item_text(item)
            # Find this item in scored results
            match = next((s for s in scored if s["text"][:120] == text[:120]), None)
            if match and match["total"] > 0 and len(capped) < MAX_QUEUE_SIZE:
                capped.append(item)
            elif match:
                excess.append(item)
            else:
                # Not in scored results — include if under cap
                if len(capped) < MAX_QUEUE_SIZE:
                    capped.append(item)
                else:
                    excess.append(item)
    except Exception:
        # Fallback: just cap by length (longer = more specific = keep)
        deduped.sort(key=lambda x: len(get_item_text(x)), reverse=True)
        capped = deduped[:MAX_QUEUE_SIZE]
        excess = deduped[MAX_QUEUE_SIZE:]

    # Summary
    print(f"Original queue: {original_size} items")
    print(f"  Resolved removed: {len(resolved)}")
    print(f"  Duplicates removed: {len(removed_dupes)}")
    print(f"  Excess dropped: {len(excess)}")
    print(f"  Final queue: {len(capped)} items")
    print()

    if resolved:
        print("RESOLVED ITEMS (removed):")
        for item in resolved[:10]:
            print(f"  - {get_item_text(item)[:70]}")
        print()

    if removed_dupes:
        print("DUPLICATE PAIRS (second copy removed):")
        for item in removed_dupes[:10]:
            print(f"  - {get_item_text(item)[:70]}")
        print()

    if excess:
        print("EXCESS ITEMS (lowest score, dropped):")
        for item in excess[:10]:
            print(f"  - {get_item_text(item)[:70]}")
        print()

    # Thread distribution of final queue
    threads = Counter(classify_thread(get_item_text(item)) for item in capped)
    print("FINAL QUEUE THREAD DISTRIBUTION:")
    for thread, count in threads.most_common():
        print(f"  {thread:15s}: {count}")

    if not dry_run:
        state["curiosity_queue"] = capped
        save_state(state)
        print(f"\nQueue cleaned and saved to {SELF_STATE_PATH}")
    else:
        print(f"\nDRY RUN — no changes written. Use --apply to save.")

    return capped


if __name__ == "__main__":
    dry_run = "--apply" not in sys.argv
    state = load_state()
    cleanup_queue(state, dry_run=dry_run)
