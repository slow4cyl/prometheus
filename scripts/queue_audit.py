#!/usr/bin/env python3
"""
queue_audit.py — Monitor for items stuck in the queue.

Flags items that have been in the queue for >N cycles without being
ranked or selected. Catches silent kills like the is_already_answered
bug that was filtering ANALOGICAL items.

Run via cron every 30 minutes, or manually.

Tracks cycle count per item via a separate audit file.
"""
import json
import os
import sys
from datetime import datetime, timezone

SELF_STATE_PATH = os.path.expanduser("~/.hermes/self_state.json")
AUDIT_PATH = os.path.expanduser("~/.hermes/queue_audit.json")
STALE_THRESHOLD = 10  # Flag items older than this many cycles

def load_audit():
    """Load audit state (cycle counts per item)."""
    if os.path.exists(AUDIT_PATH):
        with open(AUDIT_PATH) as f:
            return json.load(f)
    return {"cycle_counts": {}, "last_run": None, "alerts": []}

def save_audit(audit):
    """Save audit state."""
    audit["last_run"] = datetime.now(timezone.utc).isoformat()
    with open(AUDIT_PATH, "w") as f:
        json.dump(audit, f, indent=2)

def get_item_key(item):
    """Generate a stable key for a queue item."""
    if isinstance(item, dict):
        return item.get("text", str(item))[:100]
    return str(item)[:100]

def main():
    if not os.path.exists(SELF_STATE_PATH):
        print("ERROR: self_state.json not found")
        return

    with open(SELF_STATE_PATH) as f:
        state = json.load(f)

    queue = state.get("curiosity_queue", [])
    audit = load_audit()
    cycle_counts = audit.get("cycle_counts", {})
    alerts = []

    # Increment cycle count for all current queue items
    current_keys = set()
    for item in queue:
        key = get_item_key(item)
        current_keys.add(key)
        cycle_counts[key] = cycle_counts.get(key, 0) + 1

    # Remove items no longer in queue
    stale_keys = set(cycle_counts.keys()) - current_keys
    for key in stale_keys:
        del cycle_counts[key]

    # Flag stale items
    for key, count in cycle_counts.items():
        if count >= STALE_THRESHOLD:
            # Find the full item for context
            for item in queue:
                if get_item_key(item) == key:
                    is_analogical = isinstance(item, dict) and item.get("experiment_type") == "ANALOGICAL"
                    alert = {
                        "text": key[:80],
                        "cycles": count,
                        "is_analogical": is_analogical,
                        "experiment_type": item.get("experiment_type", "unknown") if isinstance(item, dict) else "unknown",
                        "priority": item.get("priority", "?") if isinstance(item, dict) else "?",
                    }
                    alerts.append(alert)
                    break

    # Save audit state
    audit["cycle_counts"] = cycle_counts
    audit["alerts"] = alerts
    save_audit(audit)

    # Report
    if alerts:
        print(f"=== QUEUE AUDIT: {len(alerts)} STALE ITEMS (>={STALE_THRESHOLD} cycles) ===\n")
        for a in sorted(alerts, key=lambda x: -x["cycles"]):
            tag = " ★ ANALOGICAL" if a["is_analogical"] else ""
            print(f"  [{a['cycles']:3d} cycles] ({a['experiment_type']}) {a['text']}...{tag}")
        print()

        # Highlight ANALOGICAL items specifically
        analogical_stale = [a for a in alerts if a["is_analogical"]]
        if analogical_stale:
            print(f"⚠ {len(analogical_stale)} ANALOGICAL items stuck — likely filtered by a type-unaware check")
    else:
        print(f"Queue audit OK: {len(queue)} items, none stale (threshold={STALE_THRESHOLD} cycles)")

    return len(alerts)

if __name__ == "__main__":
    # This is a REPORT, not a pass/fail gate. Under the no_agent cron contract a
    # non-zero exit is treated as a job FAILURE (error alert), so returning 1
    # whenever stale items exist (almost always) produced a permanent false
    # "error" state and alert fatigue. Findings are surfaced via stdout + the
    # queue_audit.json file; always exit 0 so cron only flags genuine crashes.
    try:
        main()
        sys.exit(0)
    except Exception as e:
        # A real crash SHOULD alert.
        print(f"queue_audit.py CRASHED: {e}", file=sys.stderr)
        sys.exit(1)
