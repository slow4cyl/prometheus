#!/usr/bin/env python3
"""adversarial_replication_enqueuer.py — attack claims before they reach ESTABLISHED.

Why (2026-07-01 audit): "replication" in this system meant another worker
agreeing — and a replication that re-derives the same circular construction
replicates the flaw. compute_maturity() now requires
n_break_survivals >= MATURITY_THRESHOLDS['established_break_survivals'] for
ESTABLISHED (see maturity.py). This script supplies those attacks: for each
REPLICATED claim with no adversarial replication yet, it enqueues ONE
break-lane kanban task whose card contains the claim, its supporting
findings, pointers to archived code (artifact_preserve.py), and explicit
instructions to REFUTE.

Outcome routing lives in apply_worker_results.py: the card body carries
"ADVERSARIAL_REPLICATION_FOR_CLAIM: <id>" and the finding carries an explicit
"ATTACK_OUTCOME: BROKEN|NARROWED|SURVIVED" line. Intake resolves the pending
adversarial_replications row accordingly:
  BROKEN   -> 'refuted'  (disputes the claim; settled later by adversarial
              arbitration — see dispute_arbitration_enqueuer.py)
  NARROWED -> 'narrowed' (neutral: no dispute, no break-survival credit; the
              claim stays REPLICATED and remains attack-eligible, so the
              narrowed core gets re-attacked)
  SURVIVED -> 'survived' (counts toward established_break_survivals)
Findings without the token fall back to verdict mapping (REFUTED->refuted,
PARTIALLY REFUTED->narrowed, SUPPORTED->survived).

Caps: MAX_PENDING outstanding attacks at once; one live attempt per claim
('narrowed' and arbitration-settled rows do NOT block re-attack);
pending rows older than EXPIRE_HOURS are marked expired (lost task) and the
claim becomes eligible again.

Cross-family attackers (2026-07-03): the worker fleet is a monoculture
(xiaomi/mimo-v2.5), so a same-family attack shares the prior that produced the
claim — every "independent" retest is another draw from the same belief. Attacks
are therefore dispatched on models from OTHER training lineages via the kanban
per-task model_override column (the dispatcher passes -m <model> at spawn —
hermes_cli/kanban_db.py). Which attacker ran is recorded in
adversarial_replications.attacker_model; `--stats` compares survival rates by
attacker so cross-family vs same-family verdicts can be told apart.

Usage:
    python3 adversarial_replication_enqueuer.py [--dry-run] [--limit N]
    python3 adversarial_replication_enqueuer.py --attacker deepseek/deepseek-v4-flash
    python3 adversarial_replication_enqueuer.py --stats
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db
from circularity_critic import find_archived_code

SAFE_CREATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "safe_kanban_create.py")
KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")
MAX_PENDING = 16      # outstanding attacks (6 -> 16 for the retest-drain
                      # surge: the drain replicates ~60% of ~800 credits/day,
                      # so REPLICATED inflow is ~350-400/day while 6 slots at a
                      # 30m cadence topped out near ~115/day — the attack gate
                      # would become the new ladder bottleneck within two days.
                      # Self-throttling: targets are REPLICATED claims with no
                      # live attack row, so when the surge passes the lane
                      # idles back down on its own.)
ESTABLISHED_WSC = 3.0  # claims at/above this can promote the moment they survive
EXPIRE_HOURS = 48
SUPPORT_SNIPPETS = 3
# NARROWED-attractor terminal cap. A claim narrowed this many times without ever
# surviving is attack-saturated: its regime is thoroughly mapped in claim_scopes,
# yet each re-attack finds a fresh incidental nuance to NARROW on instead of
# converging (SURVIVED_WITHIN_SCOPE). Past the cap we stop spending attack slots on
# it — the mapped scopes ARE its finding; it stays REPLICATED (it never earned a
# clean 'survived', so ESTABLISHED is correctly withheld) and the orbit ends. Build-
# time distribution: 772 claims narrowed 1-2x (normal), 48 at 3-4x, 18 at >=5x (the
# orbiters — #65826 at 21 narrowed / 0 survived). The cap retires that >=5x tail.
MAX_NARROWS_BEFORE_TERMINAL = 5

# ── Cross-family attacker pool ──────────────────────────────────────────────
WORKER_FAMILY = "xiaomi/mimo-v2.5"   # the fleet default; attackers must differ
ATTACKER_POOL = [
    # (openrouter slug, weight) — weights are relative shares of enqueued attacks.
    # Primary: paid but cheap ($0.09/$0.18 per Mtok), 1M ctx, reasoning model;
    # the worker harness's global reasoning_effort (config.yaml) rides along.
    ("deepseek/deepseek-v4-flash", 40),
    # Local Qwen3.5 on the 5090 — free, always-on, NO daily quota (the free
    # families die at the ~1000/day cap; A1 doesn't). Cross-family vs the mimo
    # fleet. Co-primary with deepseek, which stays as the unlimited cloud surge
    # valve. pick_attacker's family-exclusion keeps A1 off any claim the qwen
    # lineage (A1 or qwen:free) already supported; the a1_router won't demote a
    # failed A1 attack to the mimo default. A1 shares the router's cap with the
    # transfer lane, so a saturated A1 just queues its share (expires+rotates at 48h).
    ("agents-a1", 40),
    # Free cross-family probes, deliberately a sliver: OpenRouter's free tier is
    # ~20 req/min and 1000 req/day ACCOUNT-WIDE (the account holds the $10+
    # credit that unlocks 1000/day), and one agentic attack task burns tens of
    # requests — so the free share is budget-bound at roughly 20-30 sessions/day
    # regardless of weights. The 20-point share is therefore split across FOUR
    # independent families (NVIDIA, OpenAI, Meta, Google) instead of two: same
    # request budget, twice the prior diversity for the survival-by-family
    # ledger (--stats). A rate-limited/stalled task fails or expires (48h),
    # the claim re-enters the pool, and pick_attacker rotates to the next model —
    # self-healing, so a dead free endpoint can never dam the attack gate.
    # At steady state (attack volume ~10x below the drain surge) the free share
    # can rise to ~40-50 points within the same 1000/day quota.
    ("nvidia/nemotron-3-ultra-550b-a55b:free", 5),
    ("openai/gpt-oss-120b:free", 5),
    ("meta-llama/llama-3.3-70b-instruct:free", 5),
    ("google/gemma-4-31b-it:free", 5),
]


# Attacker slug -> family label as recorded in claim_independence.families.
# agents-a1 (local Qwen3.5) and qwen/*:free share the "qwen" lineage, so either
# one supporting a claim excludes A1 from attacking it.
ATTACKER_FAMILY = {
    "deepseek/deepseek-v4-flash": "deepseek",
    "agents-a1": "qwen",
    "nvidia/nemotron-3-ultra-550b-a55b:free": "nemotron",
    "openai/gpt-oss-120b:free": "openai",
    "meta-llama/llama-3.3-70b-instruct:free": "llama",
    "google/gemma-4-31b-it:free": "gemma",
}


def supporting_families(conn, claim_id):
    """Families that already back the claim — the mimo fleet always (a REPLICATED
    claim is mimo-supported by definition), plus whatever the pre-computed
    independence gate recorded (claim_independence.families). An attacker from any
    of these would be grading its own lineage's work, so pick_attacker skips them.
    This is what keeps A1 (qwen) off a claim its own lineage helped support."""
    fams = {"mimo"}
    try:
        row = conn.execute(
            "SELECT families FROM claim_independence WHERE claim_id=?",
            (claim_id,)).fetchone()
        if row and row["families"]:
            fams.update(f.strip() for f in str(row["families"]).split(",") if f.strip())
    except Exception:
        pass
    return fams


# The two attackers reliable enough for targeted (--claim) challenges: the paid
# cloud primary + the always-on local A1. The budget-bound free tier is excluded
# from targeted work unless independence leaves nothing else.
RELIABLE_ATTACKERS = ("deepseek/deepseek-v4-flash", "agents-a1")


def pick_attacker(conn, claim_id, forced=None, reliable_only=False):
    """Deterministic weighted pick that rotates on each re-attack of a claim AND
    excludes any attacker whose family already supports the claim (independence).

    Seed = (claim_id, prior attack-row count): same claim + same attempt always
    picks the same attacker (reproducible), while a claim whose attack expired
    or came back narrowed draws afresh next round instead of hammering the same
    rate-limited free endpoint forever. The family-exclusion means A1 never grades
    a claim its own (qwen) lineage helped support — the whole point of cross-family.

    reliable_only=True (the targeted --claim lanes) restricts the pool to
    RELIABLE_ATTACKERS *before* the exclusion. This replaced a hard
    `ATTACKER_POOL[0][0]` force that sent EVERY targeted challenge to deepseek,
    bypassing the independence gate entirely — deepseek was re-examining claims
    its own survived attacks corroborated, and A1 (40-weight co-primary) got
    exactly zero targeted picks. Fallback order when independence excludes the
    whole restricted pool: free-tier families that don't back the claim (an
    independent flaky attacker beats a reliable self-grading one), then any
    non-mimo pool entry as the last resort.
    """
    if forced:
        return forced
    excluded = supporting_families(conn, claim_id)
    pool = ([(s, w) for s, w in ATTACKER_POOL if s in RELIABLE_ATTACKERS]
            if reliable_only else ATTACKER_POOL)
    eligible = [(s, w) for s, w in pool
                if ATTACKER_FAMILY.get(s, s) not in excluded]
    if not eligible and reliable_only:  # restricted pool fully excluded — widen to independent free tier
        eligible = [(s, w) for s, w in ATTACKER_POOL
                    if ATTACKER_FAMILY.get(s, s) not in excluded]
    if not eligible:  # every pool family already touched it — drop only the worker family
        eligible = [(s, w) for s, w in ATTACKER_POOL if ATTACKER_FAMILY.get(s, s) != "mimo"]
    attempt = conn.execute(
        "SELECT COUNT(*) AS c FROM adversarial_replications WHERE claim_id=?",
        (claim_id,)).fetchone()["c"]
    seed = int(hashlib.md5(f"{claim_id}:{attempt}".encode()).hexdigest()[:8], 16)
    tick = seed % sum(w for _, w in eligible)
    for slug, weight in eligible:
        if tick < weight:
            return slug
        tick -= weight
    return eligible[0][0]


def set_model_override(task_id, model):
    """Stamp the per-task model override the dispatcher passes as -m at spawn.

    `hermes kanban create` has no flag for it, so this is a direct one-row
    UPDATE on kanban.db right after creation. If it fails the task simply runs
    on the fleet default — same as every attack before this feature.
    """
    try:
        k = sqlite3.connect(KANBAN_DB, timeout=10)
        k.execute("PRAGMA busy_timeout=10000")
        cur = k.execute("UPDATE tasks SET model_override=? WHERE id=?",
                        (model, task_id))
        k.commit()
        ok = cur.rowcount == 1
        k.close()
        return ok
    except Exception as e:
        print(f"  WARN: model_override failed for {task_id}: {e}")
        return False


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS adversarial_replications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            kanban_task_id TEXT,
            experiment_id TEXT,
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending|survived|refuted|expired
            resolved_at REAL,
            notes TEXT
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_advrep_claim
        ON adversarial_replications(claim_id, status)""")
    try:
        conn.execute(
            "ALTER TABLE adversarial_replications ADD COLUMN attacker_model TEXT")
    except Exception:
        pass  # column already exists
    conn.commit()


def expire_stale(conn, dry_run):
    cutoff = time.time() - EXPIRE_HOURS * 3600
    stale = conn.execute(
        "SELECT id, claim_id FROM adversarial_replications "
        "WHERE status = 'pending' AND created_at < ?", (cutoff,)).fetchall()
    for row in stale:
        print(f"  expiring stale attack #{row['id']} (claim {row['claim_id']})")
        if not dry_run:
            conn.execute(
                "UPDATE adversarial_replications SET status='expired', "
                "resolved_at=?, notes='no result within expiry window' WHERE id=?",
                (time.time(), row["id"]))
    if stale and not dry_run:
        conn.commit()
    return len(stale)


def adv_exp_id(claim_id):
    """Distinct experiment-id namespace for adversarial replications:
    exp_adv_<claim>_<utc-stamp>. Unique by construction, never collides with
    the numeric exp_<N> chain other generators max() over."""
    return f"exp_adv_{claim_id}_{time.strftime('%y%m%d%H%M', time.gmtime())}"


def make_body(exp_id, claim, supports, code_paths, attacker, note="", targeted=False, scopes=None):
    support_lines = "\n".join(
        f"- [{s['experiment_id']}] {str(s['finding'])[:400]}" for s in supports)
    code_lines = ("\n".join(f"- {p}" for p in code_paths)
                  if code_paths else "- (none preserved — reconstruct from the findings)")
    hyp = (claim["hypothesis_text"] or "").strip()
    cross_note = ""
    if attacker != WORKER_FAMILY:
        cross_note = f"""
CROSS-FAMILY ATTACK: You are running as {attacker} — deliberately a DIFFERENT
model family from the {WORKER_FAMILY} workers that produced every supporting
finding below. Their shared training prior is part of the attack surface: do
not trust their framing, their choice of test, or their interpretation. If your
own prior about this claim disagrees with theirs, that disagreement is signal —
test it instead of deferring.
"""
    # A targeted attack re-examines a claim that ALREADY reached its tier (often
    # by surviving a same-family attack) because a specific external challenge
    # was raised against it — so the generic "not yet promoted" framing is wrong.
    if targeted:
        context = (f"CONTEXT: This claim is already {claim['claim_status'] or 'promoted'}, "
                   "and survived its earlier attacks — but those attackers shared the "
                   "workers' training prior. A SPECIFIC challenge (below) has been raised "
                   "that the original tests did not confront. Your job is to settle it by "
                   "independent computation. A genuine break RE-OPENS a promoted claim; an "
                   "honest survival against this specific challenge is the strongest support "
                   "it can get. Do NOT rubber-stamp — and do NOT defer to the paper or the "
                   "prior; run the test.")
    else:
        context = ("CONTEXT: The claim below is REPLICATED (multiple supporting experiments) "
                   "and will be promoted to ESTABLISHED only if it SURVIVES this attack. Your "
                   "job is to REFUTE it. You succeed as a scientist either way: a genuine break "
                   "is a discovery, and an honest failed attack is the strongest support the "
                   "claim can get. Do NOT rubber-stamp.")
    challenge = f"\nSPECIFIC CHALLENGE TO SETTLE (attack this first):\n{note.strip()}\n" if note.strip() else ""
    # Scope-aware re-attack: a prior attack NARROWED this claim and a boundary-
    # mapping experiment pinned down the regime. Baking that scope into the card
    # is what stops the attack->NARROWED->attack loop re-discovering the same
    # out-of-regime hole (claims were re-NARROWED up to 21x): the attacker must
    # attack WITHIN the mapped regime, and only a NEW boundary counts as NARROWED.
    scope_block = ""
    scope_outcome = ""
    scope_choice = ""
    if scopes:
        scope_lines = "\n".join(f"- {str(s)[:600]}" for s in scopes)
        scope_block = f"""
MAPPED SCOPE (a prior attack NARROWED this claim; a boundary-mapping experiment
then pinned down where it holds — this regime is ALREADY KNOWN):
{scope_lines}

Attack the claim WITHIN its mapped regime. A failure OUTSIDE that regime is
already known and is NOT a new NARROWED — do not re-report it. Report NARROWED
only for a NEW boundary that MATERIALLY SHRINKS the mapped regime.
"""
        # Convergence semantics: without this outcome, any incidental nuance
        # pulls the verdict to NARROWED ("no meaningful narrowing" reads as
        # absolute), and a claim whose core keeps holding orbits the
        # narrow->map->re-attack cycle forever instead of promoting.
        scope_outcome = """  ATTACK_OUTCOME: SURVIVED_WITHIN_SCOPE  (scoped claims only: the core held WITHIN
                              the mapped scope against your best attacks; any newly
                              found limit is INCIDENTAL — state it in the finding)
"""
        scope_choice = (" For THIS scoped claim, prefer SURVIVED_WITHIN_SCOPE over "
                        "NARROWED when the core holds inside the mapped scope and "
                        "your new limit does not materially shrink it.")
    tok_menu = ("BROKEN|NARROWED|SURVIVED_WITHIN_SCOPE|SURVIVED" if scopes
                else "BROKEN|NARROWED|SURVIVED")
    return f"""ADVERSARIAL REPLICATION — try to BREAK this claim.

{context}
{cross_note}{challenge}{scope_block}

HYPOTHESIS: {hyp}

SUPPORTING FINDINGS (what you are attacking):
{support_lines}

ORIGINAL EXPERIMENT CODE (preserved artifacts — read before attacking):
{code_lines}

METHOD (attack surface, in order of power):
1. CIRCULARITY: is the target/label constructed from the same features used to detect it,
   or the data synthesized to contain the tested property? If so, build an INDEPENDENT
   construction (labels from a source that does not share features with the detector) and
   re-test. Circular support collapsing under independent construction = REFUTED.
2. ASSUMPTIONS: identify the load-bearing assumptions (distributions, parameter ranges,
   noise model) and perturb them. If the effect needs a knife-edge setting, report it.
3. VARIANCE: re-run the core computation with different seeds/samples. If the reported
   effect is within noise, it is not an effect.
4. BOUNDARIES: push the claimed relationship outside the tested range — does it invert?

EXPECTED — your finding MUST contain exactly one of these outcome lines:
  ATTACK_OUTCOME: BROKEN     (the claim's CORE mechanism failed your test — say exactly how)
  ATTACK_OUTCOME: NARROWED   (the core survives, but only in a narrower regime than
                              claimed — STATE the surviving core AND the boundary you found)
{scope_outcome}  ATTACK_OUTCOME: SURVIVED   (it survived your best attacks — list which attacks)
Choose BROKEN only when the central claimed relationship fails; a boundary or scope
finding is NARROWED, not BROKEN. Choose SURVIVED only with no meaningful narrowing.{scope_choice}

ADVERSARIAL_REPLICATION_FOR_CLAIM: {claim['id']}

RESULT WRITING (MANDATORY — run BEFORE kanban_complete):
  python3 ~/.hermes/scripts/write_worker_result.py \\
    --experiment {exp_id} \\
    --finding "ATTACK_OUTCOME: <{tok_menu}>. What your attack found. WHY: mechanism. ADVERSARIAL_REPLICATION_FOR_CLAIM: {claim['id']}" \\
    --refuted-or-supported-flag-here --confidence 0.XX --domain auto \\
    --tags ADVERSARIAL_REPLICATION --basis independent_computation \\
    --model "{attacker}" \\
    --files "{exp_id}.py,{exp_id}_results.json"
(use --refuted for BROKEN, --supported for any survived/narrowed outcome; keep BOTH the
ATTACK_OUTCOME and ADVERSARIAL_REPLICATION_FOR_CLAIM lines in the finding so intake
can route the outcome)
"""


def get_targets(conn, limit):
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM adversarial_replications WHERE status='pending'"
    ).fetchone()["c"]
    slots = max(0, MAX_PENDING - pending)
    if slots == 0:
        print(f"{pending} attack(s) already pending — no free slots.")
        return []
    rows = conn.execute("""
        SELECT kc.id, kc.hypothesis_text,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'REPLICATED'
          AND COALESCE(kc.circular_construction, 0) = 0
          AND COALESCE(kc.is_meta, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM adversarial_replications ar
              WHERE ar.claim_id = kc.id AND ar.status IN ('pending', 'survived', 'refuted'))
          -- Boundary coupling: a claim with an OPEN boundary-mapping question
          -- (spawned when an attack NARROWED it) is not re-attacked until a
          -- worker maps the boundary — the answer informs the next attack and
          -- stops attack->narrowed->attack loops burning slots on the same
          -- unmapped regime.
          AND NOT EXISTS (
              SELECT 1 FROM curiosities bc
              WHERE bc.provenance = 'narrowed_boundary' AND bc.status = 'active'
                AND bc.text LIKE '[BOUNDARY] Claim ' || kc.id || ' %')
          -- NARROWED-attractor terminal cap: a claim narrowed this many times with
          -- no 'survived' is attack-saturated — its regime is mapped and further
          -- attacks only re-discover incidental nuance. Retire it from the lane
          -- rather than orbiting narrow->boundary->re-attack forever (#65826: 21/0).
          AND (SELECT COUNT(*) FROM adversarial_replications ar3
               WHERE ar3.claim_id = kc.id AND ar3.status = 'narrowed') < ?
        ORDER BY (COALESCE(kc.weighted_support_count,0) >= ?) DESC,
                 kc.weighted_support_count DESC
        LIMIT ?""", (MAX_NARROWS_BEFORE_TERMINAL, ESTABLISHED_WSC,
                     min(slots, limit))).fetchall()
    # Honest count of what the cap UNIQUELY retires: a survived/refuted row already
    # excludes a claim above, so the cap only newly-removes REPLICATED claims that
    # never survived yet keep narrowing (the true orbiters — the #65826 shape). This
    # is mostly a FORWARD guard: an orbiter usually wsc-decays to CANDIDATE before it
    # reaches 21, but the cap stops the next one from ever orbiting past the limit.
    saturated = conn.execute(
        "SELECT COUNT(*) AS c FROM (SELECT ar.claim_id FROM adversarial_replications ar "
        "JOIN knowledge_claims kc ON kc.id = ar.claim_id "
        "WHERE kc.claim_status='REPLICATED' "
        "GROUP BY ar.claim_id "
        "HAVING SUM(ar.status='narrowed') >= ? AND SUM(ar.status IN ('survived','refuted'))=0)",
        (MAX_NARROWS_BEFORE_TERMINAL,)).fetchone()["c"]
    if saturated:
        print(f"  {saturated} REPLICATED claim(s) attack-saturated "
              f"(>={MAX_NARROWS_BEFORE_TERMINAL} narrows, 0 survived) — retired from the lane.")
    return rows


def get_targets_by_id(conn, claim_ids):
    """Explicit-claim attacks — bypass the REPLICATED filter so an ALREADY-PROMOTED
    claim can be re-examined against a specific external challenge (a literature
    contradiction, a theorem it seems to violate). Only skips claims that already
    have a LIVE (pending) attack; a prior 'survived'/'narrowed'/'refuted' row does
    NOT block — that same-family survival is exactly what we're re-testing."""
    out = []
    for cid in claim_ids:
        row = conn.execute("""
            SELECT kc.id, kc.hypothesis_text, kc.claim_status,
                   COALESCE(kc.weighted_support_count, 0) AS wsc
            FROM knowledge_claims kc WHERE kc.id = ?""", (cid,)).fetchone()
        if not row:
            print(f"  claim {cid}: not found — skipping")
            continue
        live = conn.execute(
            "SELECT COUNT(*) AS c FROM adversarial_replications "
            "WHERE claim_id = ? AND status = 'pending'", (cid,)).fetchone()["c"]
        if live:
            print(f"  claim {cid}: already has a live attack — skipping")
            continue
        out.append(row)
    return out


def print_stats(conn):
    """Attack outcomes by attacker model — the cross- vs same-family ledger."""
    print("attack outcomes by attacker model (intended at enqueue):")
    for r in conn.execute("""
            SELECT COALESCE(attacker_model, '(legacy/mimo)') AS atk, status, COUNT(*) AS n
            FROM adversarial_replications GROUP BY 1, 2 ORDER BY 1, 2"""):
        print(f"  {r['atk']:45s} {r['status']:20s} {r['n']}")
    print("\nsurvival rate (survived / (survived + refuted[+arbitrated])); narrowed is neutral:")
    for r in conn.execute("""
            SELECT COALESCE(attacker_model, '(legacy/mimo)') AS atk,
                   SUM(status='survived') AS s,
                   SUM(status IN ('refuted','refuted_arbitrated')) AS b,
                   SUM(status='narrowed') AS nar,
                   SUM(status='pending') AS pend
            FROM adversarial_replications GROUP BY 1 ORDER BY 1"""):
        denom = (r["s"] or 0) + (r["b"] or 0)
        rate = f"{100.0 * (r['s'] or 0) / denom:5.1f}%" if denom else "   —  "
        print(f"  {r['atk']:45s} {rate}  (survived={r['s']}, broken={r['b']}, "
              f"narrowed={r['nar']}, pending={r['pend']})")
    rows = conn.execute("""
            SELECT ar.attacker_model AS intended, wr.model AS actual, COUNT(*) AS n
            FROM adversarial_replications ar
            JOIN worker_results wr ON wr.experiment_id = ar.experiment_id
            WHERE ar.attacker_model IS NOT NULL
            GROUP BY 1, 2 ORDER BY 3 DESC""").fetchall()
    if rows:
        print("\nintended vs actually-executed model (worker_results.model):")
        for r in rows:
            flag = "" if (r["actual"] or "").strip() == r["intended"] else "  <-- MISMATCH"
            print(f"  {r['intended']:45s} ran as {r['actual'] or '(unset)'} x{r['n']}{flag}")


def main():
    ap = argparse.ArgumentParser(description="Enqueue adversarial replications for REPLICATED claims")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=MAX_PENDING)
    ap.add_argument("--attacker", default=None,
                    help="Force one attacker model for every attack enqueued this run")
    ap.add_argument("--stats", action="store_true",
                    help="Print per-attacker outcome ledger and exit")
    ap.add_argument("--claim", type=int, action="append", default=[],
                    help="Attack this specific claim id regardless of tier (repeatable). "
                         "For re-examining an already-promoted claim against a challenge.")
    ap.add_argument("--note", default="",
                    help="Specific challenge injected into the card (e.g. a literature "
                         "contradiction) — only used with --claim")
    args = ap.parse_args()

    conn = get_db()
    ensure_table(conn)
    if args.stats:
        print_stats(conn)
        conn.close()
        return 0

    targeted = bool(args.claim)
    if targeted:
        targets = get_targets_by_id(conn, args.claim)
    else:
        expire_stale(conn, args.dry_run)
        targets = get_targets(conn, args.limit)
    if not targets:
        print("No claims to attack." if targeted else
              "No REPLICATED claims awaiting adversarial replication.")
        conn.close()
        return 0

    hermes = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    created = 0
    for claim in targets:
        supports = conn.execute("""
            SELECT ce.experiment_id,
                   COALESCE(NULLIF(TRIM(ce.key_finding), ''), e.result) AS finding,
                   e.kanban_task_id
            FROM claim_evidence ce
            LEFT JOIN experiments e ON e.id = ce.experiment_id
            WHERE ce.claim_id = ? AND COALESCE(ce.evidence_type,'support')='support'
            ORDER BY ce.confidence DESC, ce.id DESC LIMIT ?""",
            (claim["id"], SUPPORT_SNIPPETS)).fetchall()
        supports = [s for s in supports if s["finding"]]
        if not supports:
            continue
        code_paths = []
        for s in supports:
            p, _ = find_archived_code(hermes, s["experiment_id"], s["kanban_task_id"])
            if p:
                code_paths.append(p)

        # Latest mapped scopes for this claim (claim_scopes is written at
        # boundary-task closure in apply_worker_results.py; table may not
        # exist yet on a fresh DB — treat that as "no scope").
        try:
            _scopes = [r["scope_text"] for r in conn.execute(
                "SELECT scope_text FROM claim_scopes WHERE claim_id = ? "
                "ORDER BY id DESC LIMIT 2", (claim["id"],)).fetchall()]
        except Exception:
            _scopes = []

        exp_id = adv_exp_id(claim["id"])
        # Targeted re-examinations get a RELIABLE attacker (deepseek or local A1)
        # through the independence-gated pick — the old hard force to
        # ATTACKER_POOL[0][0] sent every targeted challenge to deepseek, even on
        # claims deepseek's own survived attacks corroborated, and starved A1
        # (0 of 341 attacks in 48h despite a 40 pool weight).
        forced = args.attacker or None
        attacker = pick_attacker(conn, claim["id"], forced=forced,
                                 reliable_only=bool(targeted))
        tag = "[RE-EXAMINE]" if targeted else "[ADVERSARIAL]"
        title = f"{exp_id}: {tag} Break claim #{claim['id']}: {(claim['hypothesis_text'] or '')[:70]}"
        body = make_body(exp_id, claim, supports, code_paths, attacker,
                         note=args.note, targeted=targeted, scopes=_scopes)
        if args.dry_run:
            print(f"[DRY-RUN] would enqueue attack on claim {claim['id']} "
                  f"(wsc={claim['wsc']:.1f}, code_paths={len(code_paths)}, "
                  f"attacker={attacker}, targeted={targeted}): {title[:100]}")
            continue

        r = subprocess.run(
            [sys.executable, SAFE_CREATE, title, "--assignee", "default",
             # p4: above the retest flood — attacks/arbitration are the ladder's top
             # and must not starve behind ~1k p3 retest tasks.
             "--priority", "4", "--body", body],
            capture_output=True, text=True, timeout=60)
        m = re.search(r"t_[a-f0-9]+", r.stdout or "")
        if r.returncode == 0 and m:
            task_id = m.group(0)
            # Stamp the dispatcher override; on failure the task runs on the
            # fleet default, and we record THAT so stats never lie.
            if attacker != WORKER_FAMILY and not set_model_override(task_id, attacker):
                attacker = WORKER_FAMILY
            conn.execute("""
                INSERT INTO adversarial_replications
                (claim_id, kanban_task_id, experiment_id, created_at, status, attacker_model)
                VALUES (?, ?, ?, ?, 'pending', ?)""",
                (claim["id"], task_id, exp_id, time.time(), attacker))
            conn.commit()
            created += 1
            print(f"ENQUEUED attack on claim {claim['id']} -> {task_id} ({exp_id}) "
                  f"attacker={attacker}")
        else:
            print(f"  create failed for claim {claim['id']}: "
                  f"rc={r.returncode} {(r.stdout or '')[:200]} {(r.stderr or '')[:200]}")

    print(f"\nEnqueued {created} adversarial replication(s)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
