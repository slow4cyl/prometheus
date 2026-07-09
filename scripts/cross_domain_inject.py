#!/usr/bin/env python3
"""
cross_domain_inject.py — SELF-GENERATING cross-domain novelty injection.

Replaces the old hardcoded 20-hypothesis list (which exhausted itself: once run,
every future call re-emitted the same 20 and they all blocked as duplicates —
a dead loop for an "autonomous" system).

This version MINES THE SYSTEM'S OWN ACCUMULATED KNOWLEDGE to generate fresh
cross-domain curiosities, closing the loop:
  confirmed mechanisms (from saturated domains)
    × under-explored / frontier domains
    → "does this mechanism also operate in that domain?" hypotheses
    → experiments → new findings/domains → new mechanisms → repeat.

Because the source mechanisms and target domains GROW as the knowledge base
grows, the generator never runs dry the way a static list does. Each generated
hypothesis is deduped against titles already on the board, so repeats are skipped
and the generator naturally reaches for new combinations over time.

Usage:
  python3 cross_domain_inject.py [--count N] [--dry-run]
"""
import argparse
import fcntl
import json
import os
import random
import re
import subprocess
import sys

PROM_DB = os.path.expanduser("~/.hermes/prometheus.db")
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
SAFE_CREATE = os.path.expanduser("~/.hermes/scripts/safe_kanban_create.py")
LOCK_PATH = os.path.expanduser("~/.hermes/cross_domain_inject.lock")
import sqlite3

# Make db_retry importable (same scripts dir)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def acquire_lock():
    """Single-instance guard — same pattern as the other script-only crons.
    If a previous cycle is still running, exit cleanly rather than overlap."""
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except IOError:
        print("Another cross_domain_inject instance running, exiting.", file=sys.stderr)
        sys.exit(0)


def _conn(path):
    """prometheus.db reads go through db_retry (retry-on-lock under fleet
    contention); kanban reads fall back to a plain timeout connection."""
    if path == PROM_DB:
        try:
            from db_retry import get_db
            return get_db(path)
        except Exception:
            pass
    c = sqlite3.connect(path, timeout=10)
    c.execute("PRAGMA busy_timeout=5000")
    return c


def next_exp_id():
    db = _conn(KANBAN_DB)
    rows = db.execute("SELECT title FROM tasks WHERE title GLOB 'exp_[0-9]*'").fetchall()
    db.close()
    ids = [int(m.group(1)) for (t,) in rows if (m := re.search(r"exp_(\d+)", t))]
    return (max(ids) + 1) if ids else 1


def existing_titles_lower():
    """Lowercased non-archived task titles, to avoid regenerating duplicates."""
    db = _conn(KANBAN_DB)
    rows = db.execute("SELECT lower(title) FROM tasks WHERE status != 'archived'").fetchall()
    db.close()
    return {r[0] for r in rows}


def mine_mechanisms(limit=400):
    """Confirmed 'X predicts/affects Y' findings — the transferable mechanisms.

    Source = the system's own completed experiments. We grab a clean predictive
    clause from the hypothesis text and the domain it was found in.
    """
    db = _conn(PROM_DB)
    rows = db.execute(
        "SELECT domain, hypothesis FROM experiments "
        "WHERE status='completed' AND hypothesis IS NOT NULL "
        "AND (result LIKE 'CONFIRMED%' OR result LIKE 'SUPPORTED%' OR result LIKE '%strongly predict%') "
        "AND length(hypothesis) BETWEEN 25 AND 160 "
        "ORDER BY RANDOM() LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    mechs = []
    for domain, hyp in rows:
        h = hyp.strip()
        # Strip leading bracket tags and question framing.
        h = re.sub(r"^\[[^\]]*\]\s*", "", h)
        h = re.sub(r"^(does|can|do|is|are|will)\s+", "", h, flags=re.I).strip()
        # Cut at the first clause boundary so we keep ONE clean mechanism.
        h = re.split(r"[?;]|\bMeasur|\balso\b", h, maxsplit=1)[0].strip().rstrip(",.")
        # Keep mechanism-shaped clauses (predict/affect/drive/govern/determine).
        if not re.search(r"\b(predict|affect|drive|govern|determine|correlat|influenc|control)\w*\b", h, re.I):
            continue
        if 15 <= len(h) <= 130:
            mechs.append((domain or "unknown", h))
    return mechs


def target_domains():
    """Domains weighted toward the FRONTIER (few experiments) so injection
    expands the knowledge graph instead of piling onto saturated hubs.

    Downweights ML-internal / infra domains — a mechanism transferring into
    'ensemble' or 'ml_theory' isn't a real cross-DOMAIN test; we favour
    real-world scientific/engineering domains for genuine transfer."""
    INFRA = {
        "calibration", "injection_detection", "adversarial_ml", "adversarial_detection",
        "ml_theory", "ml_security", "ensemble", "ensemble_methods", "tfidf", "lexical",
        "metrics_benchmarking", "dim_reduction", "interpretability", "bias",
        "cross_domain_prediction", "regulatory", "agent_systems", "dispatch_pipeline",
        "mlops", "ml-safety", "machine_learning", "novelty_generation",
    }
    db = _conn(PROM_DB)
    rows = db.execute(
        "SELECT domain, COUNT(*) c FROM experiments "
        "WHERE domain IS NOT NULL AND domain!='' GROUP BY domain"
    ).fetchall()
    db.close()
    # weight = 1/(count+1) so 1-experiment domains dominate the draw; infra
    # domains get a small fixed weight so they're rare but not impossible.
    weighted = []
    for domain, c in rows:
        if domain in INFRA:
            weighted.extend([domain] * 1)
        else:
            weighted.extend([domain] * max(2, int(round(60.0 / (c + 1)))))
    return weighted, {d: c for d, c in rows}


def generate(count):
    mechs = mine_mechanisms()
    targets, counts = target_domains()
    if not mechs or not targets:
        return []
    seen = existing_titles_lower()
    out = []
    attempts = 0
    while len(out) < count and attempts < count * 30:
        attempts += 1
        src_domain, mech = random.choice(mechs)
        tgt = random.choice(targets)
        if tgt == src_domain:
            continue
        # Frame the mechanism as a cross-domain transfer question into target.
        hyp = f"Does the mechanism '{mech}' also operate in {tgt.replace('_', ' ')}?"
        key = hyp.lower()[:80]
        if key in seen or any(key in t for t in seen):
            continue
        seen.add(key)
        out.append({"hyp": hyp, "domain": tgt, "src": src_domain})
    return out


def make_body(exp_id, item):
    d = item["domain"]
    return (
        f"CROSS-DOMAIN NOVELTY (self-generated from prior findings) -- {d.upper()}\n\n"
        f"HYPOTHESIS: {item['hyp']}\n\n"
        f"SOURCE MECHANISM: discovered in '{item['src']}'. This task tests whether it "
        f"transfers to {d.replace('_', ' ')} (a frontier / under-explored domain).\n\n"
        f"METHOD:\n"
        f"1. Design a computational experiment to test this transfer.\n"
        f"2. Web-search for relevant {d} data/papers/datasets.\n"
        f"3. Implement analysis (sklearn, stats) and compare to the prediction.\n\n"
        f"GPU AVAILABLE: RTX 5090 (32GB). For torch code use "
        f"`DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')` — "
        f"do NOT hardcode 'cpu'. sklearn imports are auto-GPU-accelerated.\n\n"
        f"EXPECTED: CONFIRMED (transfers) or REFUTED (mechanism is domain-specific).\n\n"
        f"RESULT WRITING (MANDATORY):\n"
        f"  python3 ~/.hermes/scripts/write_worker_result.py \\\n"
        f"    --experiment exp_{exp_id} \\\n"
        f'    --finding "WHAT you found AND WHY (mechanism)" \\\n'
        f"    --supported --confidence 0.8 --domain {d} --type ANALOGICAL \\\n"
        f"    --tags CROSS_DOMAIN,DISCOVERY,TRANSFER \\\n"
        f"    --basis independent_computation \\\n"
        f'    --files "exp_{exp_id}.py" \\\n'
        f'    --queue "[TRANSFER] follow-up?" \\\n\n'
        f"--basis states what your verdict rests on: independent_computation (you ran code/math\n"
        f"testing the claim itself) | replication | simulation (synthetic data built for this\n"
        f"test) | literature_authority (trusting a source without testing — confidence auto-\n"
        f"capped at 0.5). Save your code as exp_{exp_id}.py and list it in --files: listed\n"
        f"files are preserved to ~/.hermes/artifacts/ and keep the experiment replicable.\n"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # Single-instance guard (skip for dry-run, which is read-only/manual)
    lock_fd = None
    if not args.dry_run:
        lock_fd = acquire_lock()

    try:
        # Fleet size from the single source of truth, not a hardcoded 45.
        try:
            from worker_config import WORKER_COUNT as _FLEET
        except Exception:
            _FLEET = 20

        items = generate(args.count)
        if not items:
            print("Cross-domain: no novel hypotheses generated (no source mechanisms / targets).")
            return
        print(f"Generated {len(items)} self-derived cross-domain hypotheses.")
        if args.dry_run:
            for i, it in enumerate(items):
                print(f"  [{i}] ({it['src']} -> {it['domain']}) {it['hyp'][:90]}")
            return

        next_id = next_exp_id()
        print(f"Next experiment ID: {next_id}")
        created = 0
        for i, it in enumerate(items):
            title = f"exp_{next_id}: [TRANSFER] {it['hyp'][:80]}"
            worker = "default"
            # Cross-domain transfer exploration → P1 (exploration mass), matching the
            # tier other transfer/injection tasks get. Without --priority, safe_kanban_create
            # passes nothing and the task defaults to P0, burying it under the whole queue.
            r = subprocess.run(
                [sys.executable, SAFE_CREATE, title, "--assignee", worker,
                 "--priority", "1", "--body", make_body(next_id, it)],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                created += 1
                print(f"  [{i}] CREATED ({it['src']}->{it['domain']}): {it['hyp'][:60]}")
            else:
                try:
                    reason = json.loads(r.stdout).get("reason", "unknown")
                except Exception:
                    reason = (r.stdout or "")[:60]
                print(f"  [{i}] BLOCKED ({reason})")
            next_id += 1
        print(f"\nCross-domain: Created {created}/{len(items)}")
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


if __name__ == "__main__":
    main()
