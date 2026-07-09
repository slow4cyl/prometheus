#!/usr/bin/env python3
"""dispute_arbitration_enqueuer.py — settle contradictions with decisive experiments.

The missing back-half of the contradiction pipeline: detection works (the
verdict-level detector, answer_consistency_adjudicator.py, and adversarial
refutations all bump contradiction_count), but the counter was increment-only —
DISPUTED was a one-way door and the disputed pile only grew (measured: 4,257
world claims, 48 of them past the REPLICATED wsc bar). Meanwhile the
adjudicator stores QUOTED incompatible assertions per dispute — which is
exactly the specification for a decisive experiment.

Two dispute sources, merged by wsc so the highest-value dispute gets the next
free slot regardless of provenance:
  - 'adjudication': answer-level incompatibility pairs stored by the
    adjudicator (SIDE A/B = the quoted contradicting experiments)
  - 'replication': a retest disagreed with the original experiment (SIDE A =
    original, SIDE B = retest; findings verbatim from replication_results).
    Resolution settles the underlying rows (replication_status 'disagreed' ->
    'disagreed_arbitrated') so the contradiction recompute clears the dispute
    and the detector stops re-adding it — no masking, the evidence record
    itself is fixed.

For each eligible DISPUTED world claim this enqueues ONE arbitration task.
The card names the two sides with their quoted assertions and the preserved
code, and requires a verdict:

  A_CORRECT     — side A's answer survives a discriminating test; B's does not
  B_CORRECT     — mirror
  BOTH_WRONG    — the discriminating test refutes both answers
  REGIME_SPLIT  — each side is right in a different regime; state the boundary
                  and submit one --queue question per regime

Resolution (resolve_arbitration, called from apply_worker_results intake):
  - losing side's claim_evidence rows get evidence_type='retracted_by_arbitration'
    (recompute_weighted_support and every promotion-band query exclude them)
  - the dispute's contradiction bump (min(n_pairs, 3), matching the
    adjudicator's apply_contradiction) is subtracted from contradiction_count
  - claim_status is NOT touched directly: when the count reaches 0 the maturity
    recompute returns the claim to its evidence-determined tier — through the
    adversarial-replication gate like everything else
  - REGIME_SPLIT retracts nothing; the boundary questions arrive through the
    worker's normal --queue path

Caps: MAX_PENDING outstanding arbitrations, one live attempt per claim,
pending > EXPIRE_HOURS marked expired (claim becomes eligible again).

Usage:
    python3 dispute_arbitration_enqueuer.py [--dry-run] [--limit N]
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_retry import get_db
from circularity_critic import find_archived_code
from adversarial_replication_enqueuer import set_model_override
from spurious_agreement import direction_of, magnitudes_of, verdict_of

SAFE_CREATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "safe_kanban_create.py")

# RECOMPUTE cards are pure re-derivation (a numeric quantity, no interpretive
# judgment) — once a natural fit for a code-specialist model outside the fleet
# family. DISABLED 2026-07-07: qwen/qwen3-coder:free (a free model) fails the
# kanban completion protocol at a high baseline rate — it does the work but exits
# without calling kanban_complete — so its recompute cards churned dozens of
# spawn -> protocol-violation -> gave_up -> respawn cycles each (worsened while the
# task_janitor abandon-guard was blind to reaper 'gave_up' blocks). Recompute now
# runs on the fleet default (mimo), which closes reliably. Set RECOMPUTE_CODER back
# to a PROTOCOL-RELIABLE model to re-enable the slice; the guard below no-ops on None.
RECOMPUTE_CODER = None
RECOMPUTE_CODER_MOD = 4   # 1 in N first-attempt recompute cards (when RECOMPUTE_CODER is set)
MAX_PENDING = 8   # 4 -> 8 for the retest-drain surge: ~40% of ~800 drain
                  # credits/day disagree, so DISPUTED inflow jumps ~+400 over
                  # the drain window; 8 slots keeps the wsc-sorted top moving.
                  # (Originally 2; raised to 4 when the replication source
                  # landed and the eligible pool went 27 -> ~1,300.)
EXPIRE_HOURS = 48
FINDING_CHARS = 500


def ensure_table(conn):
    # DDL takes a write lock even for IF NOT EXISTS — skip it entirely once
    # the table exists, and retry once on transient lock contention.
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='dispute_arbitrations'").fetchone()
    if exists:
        _ensure_columns(conn)
        return
    for attempt in (0, 1):
        try:
            _create_table(conn)
            return
        except Exception:
            if attempt:
                raise
            time.sleep(3)


def _ensure_columns(conn):
    """Additive migration for the replication/adversarial dispute sources."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(dispute_arbitrations)")}
    added = False
    if "source_kind" not in cols:
        conn.execute("ALTER TABLE dispute_arbitrations "
                     "ADD COLUMN source_kind TEXT DEFAULT 'adjudication'")
        added = True
    if "rr_ids" not in cols:
        conn.execute("ALTER TABLE dispute_arbitrations ADD COLUMN rr_ids TEXT")
        added = True
    if "ar_ids" not in cols:
        conn.execute("ALTER TABLE dispute_arbitrations ADD COLUMN ar_ids TEXT")
        added = True
    if "triage" not in cols:
        # 'recompute' (a computable quantity disagreement — the card is
        # specialized to re-derive it) vs 'arbitrate' (interpretive/conditional
        # — the open-ended decisive-experiment card). NULL for pre-triage rows.
        conn.execute("ALTER TABLE dispute_arbitrations ADD COLUMN triage TEXT")
        added = True
    if "model_override" not in cols:
        # Model the card was dispatched on when it differs from the fleet
        # default (the RECOMPUTE_CODER slice). NULL = fleet default.
        conn.execute("ALTER TABLE dispute_arbitrations ADD COLUMN model_override TEXT")
        added = True
    if added:
        conn.commit()


def _create_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dispute_arbitrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id INTEGER NOT NULL,
            adjudication_id INTEGER,
            kanban_task_id TEXT,
            experiment_id TEXT,
            side_a TEXT,                  -- JSON list of experiment ids
            side_b TEXT,                  -- JSON list of experiment ids
            n_pairs INTEGER,
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
              -- pending|resolved_a|resolved_b|both_wrong|regime_split|expired
            resolved_at REAL,
            notes TEXT,
            source_kind TEXT DEFAULT 'adjudication',
              -- 'adjudication' (answer-level pairs) | 'replication' (disagreed retest)
              -- | 'adversarial' (attack reported BROKEN)
            rr_ids TEXT,                  -- JSON list of replication_results ids (replication source)
            ar_ids TEXT                   -- JSON list of adversarial_replications ids (adversarial source)
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_darb_claim
        ON dispute_arbitrations(claim_id, status)""")
    conn.commit()


def expire_stale(conn, dry_run):
    cutoff = time.time() - EXPIRE_HOURS * 3600
    stale = conn.execute(
        "SELECT id, claim_id FROM dispute_arbitrations "
        "WHERE status = 'pending' AND created_at < ?", (cutoff,)).fetchall()
    for row in stale:
        print(f"  expiring stale arbitration #{row['id']} (claim {row['claim_id']})")
        if not dry_run:
            conn.execute(
                "UPDATE dispute_arbitrations SET status='expired', resolved_at=?, "
                "notes='no result within expiry window' WHERE id=?",
                (time.time(), row["id"]))
    if stale and not dry_run:
        conn.commit()
    return len(stale)


def _id_variants(exp_id):
    """Adjudicator pairs sometimes carry bare ids ('312629') for 'exp_312629'."""
    s = str(exp_id).strip()
    out = {s}
    if s.startswith("exp_"):
        out.add(s[4:])
    else:
        out.add(f"exp_{s}")
    return out


def _split_sa_sides(supports):
    """Split a false-consensus claim's supports into arbitration sides using
    the SAME signals the SA score reads, tried in order of reliability:

    1. direction split — SIDE A = majority-direction supports, B = the rest
    2. magnitude spread — SIDE A = supports near the median magnitude (log10),
       B = the outliers (> 1 decade away)
    3. text/flag mismatch — SIDE A = supports whose text reads CONFIRMED,
       B = supports whose text reads REFUTED despite the supported flag

    supports: list of dicts with experiment_id + finding. Returns
    (side_a, side_b, pairs) — empty lists when no clean split exists (the
    claim is then skipped; SA may be riding the non_directional component,
    which no experiment can settle)."""
    import math
    sup = [(str(s["experiment_id"]), str(s["finding"] or "")) for s in supports
           if s["experiment_id"] and s["finding"]]
    if len(sup) < 2:
        return [], [], []

    def mk(a_ids, b_ids, why):
        f = dict(sup)
        pairs = [{"a": a_ids[0], "b": b,
                  "reason": (f"{why} || A: {f[a_ids[0]][:120]} "
                             f"|| B: {f[b][:120]}")} for b in b_ids[:4]]
        return sorted(a_ids), sorted(b_ids), pairs

    # 1. direction split
    dirs = {eid: direction_of(txt) for eid, txt in sup}
    groups = {}
    for eid, d in dirs.items():
        if d and d != "none":
            groups.setdefault(d, []).append(eid)
    if len(groups) >= 2:
        ranked = sorted(groups.values(), key=len, reverse=True)
        side_a = ranked[0]
        side_b = [e for g in ranked[1:] for e in g]
        return mk(side_a, side_b, "direction split among supports")

    # 2. magnitude spread (> 1 decade from the median)
    mags = {}
    for eid, txt in sup:
        vals = [v for v in magnitudes_of(txt) if v > 0]
        if vals:
            mags[eid] = math.log10(max(vals))
    if len(mags) >= 2:
        vals = sorted(mags.values())
        med = vals[len(vals) // 2]
        near = [e for e, m in mags.items() if abs(m - med) <= 0.5]
        far = [e for e, m in mags.items() if abs(m - med) > 1.0]
        if near and far:
            return mk(near, far, "effect-size magnitudes a decade apart")

    # 3. text/flag mismatch (verdict_of returns 'refute'/'confirm'/'partial'/None)
    clean = [eid for eid, txt in sup if verdict_of(txt) != "refute"]
    dirty = [eid for eid, txt in sup if verdict_of(txt) == "refute"]
    if clean and dirty:
        return mk(clean, dirty, "finding text contradicts the supported flag")

    return [], [], []


def get_targets(conn, limit):
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM dispute_arbitrations WHERE status='pending'"
    ).fetchone()["c"]
    slots = max(0, MAX_PENDING - pending)
    if slots == 0:
        print(f"{pending} arbitration(s) already pending — no free slots.")
        return []

    out = []

    # ── Source 1: adjudicator-stored incompatibility pairs (answer-level) ──
    rows = conn.execute("""
        SELECT kc.id AS claim_id, kc.hypothesis_text,
               COALESCE(kc.weighted_support_count, 0) AS wsc,
               aa.id AS adjudication_id, aa.incompatibilities
        FROM knowledge_claims kc
        JOIN answer_adjudications aa ON aa.claim_id = kc.id
             AND aa.consistent = 0
             AND aa.incompatibilities IS NOT NULL
             AND aa.incompatibilities != '[]'
        WHERE kc.claim_status = 'DISPUTED'
          AND COALESCE(kc.is_meta, 0) = 0
          AND COALESCE(kc.circular_construction, 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM dispute_arbitrations da
              WHERE da.claim_id = kc.id
                AND da.status IN ('pending', 'resolved_a', 'resolved_b',
                                  'both_wrong', 'regime_split'))
        GROUP BY kc.id
        HAVING aa.id = MAX(aa.id)
        ORDER BY kc.weighted_support_count DESC
        LIMIT ?""", (limit,)).fetchall()

    for row in rows:
        try:
            pairs = json.loads(row["incompatibilities"])
        except (json.JSONDecodeError, TypeError):
            continue
        pairs = [p for p in pairs
                 if isinstance(p, dict) and p.get("a") and p.get("b")]
        if not pairs:
            continue
        raw_a = {str(p["a"]) for p in pairs}
        raw_b = {str(p["b"]) for p in pairs}
        # An experiment cited on BOTH sides has ambiguous membership (it
        # contradicts each side somewhere) — exclude it from retraction
        # scope; the pairs list still shows it for the worker's context.
        overlap = raw_a & raw_b
        side_a = sorted(raw_a - overlap)
        side_b = sorted(raw_b - overlap)
        if not side_a or not side_b:
            continue  # no clean sides to arbitrate
        out.append({"source_kind": "adjudication", "row": row, "pairs": pairs,
                    "side_a": side_a, "side_b": side_b,
                    "rr_ids": None, "ar_ids": None})

    # ── Source 2: replication disagreements (retest contradicts original) ──
    # The dominant DISPUTED bucket (~94% post-recompute) — no adjudicator
    # pairs, but replication_results holds both sides' findings verbatim.
    # SIDE A = the claim's original experiment, SIDE B = disagreeing retest(s).
    # Claims that ALSO have adjudicator pairs go through source 1 (avoids
    # double-targeting and keeps the resolved-arbitration masking clean).
    rrows = conn.execute("""
        SELECT kc.id AS claim_id, kc.hypothesis_text,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'DISPUTED'
          AND COALESCE(kc.is_meta, 0) = 0
          AND COALESCE(kc.circular_construction, 0) = 0
          AND EXISTS (
              SELECT 1 FROM replication_results rr
              WHERE rr.replication_status = 'disagreed'
                AND (rr.original_experiment_id = kc.first_experiment_id
                     OR rr.original_experiment_id = kc.last_experiment_id))
          AND NOT EXISTS (
              SELECT 1 FROM answer_adjudications aa
              WHERE aa.claim_id = kc.id AND aa.consistent = 0
                AND aa.incompatibilities IS NOT NULL
                AND aa.incompatibilities != '[]')
          AND NOT EXISTS (
              SELECT 1 FROM dispute_arbitrations da
              WHERE da.claim_id = kc.id
                AND da.status IN ('pending', 'resolved_a', 'resolved_b',
                                  'both_wrong', 'regime_split'))
        ORDER BY kc.weighted_support_count DESC
        LIMIT ?""", (limit,)).fetchall()

    for row in rrows:
        rr = conn.execute("""
            SELECT rr.id, rr.original_experiment_id, rr.validation_experiment_id,
                   rr.original_finding, rr.validation_finding
            FROM replication_results rr
            JOIN knowledge_claims kc ON kc.id = ?
            WHERE rr.replication_status = 'disagreed'
              AND (rr.original_experiment_id = kc.first_experiment_id
                   OR rr.original_experiment_id = kc.last_experiment_id)
            ORDER BY rr.id DESC LIMIT 3""", (row["claim_id"],)).fetchall()
        rr = [r for r in rr if r["validation_experiment_id"]]
        if not rr:
            continue
        raw_a = {str(r["original_experiment_id"]) for r in rr}
        raw_b = {str(r["validation_experiment_id"]) for r in rr}
        overlap = raw_a & raw_b
        side_a = sorted(raw_a - overlap)
        side_b = sorted(raw_b - overlap)
        if not side_a or not side_b:
            continue
        pairs = [{"a": r["original_experiment_id"],
                  "b": r["validation_experiment_id"],
                  "reason": (f"original: {str(r['original_finding'] or '')[:140]}"
                             f" || retest: {str(r['validation_finding'] or '')[:140]}")}
                 for r in rr]
        out.append({"source_kind": "replication", "row": row, "pairs": pairs,
                    "side_a": side_a, "side_b": side_b,
                    "rr_ids": [r["id"] for r in rr], "ar_ids": None})

    # ── Source 3: adversarial breaks (attack reported BROKEN) ──
    # A refuted attack disputes the claim but a single attack worker should
    # not have unilateral kill power — a decisive experiment settles it.
    # SIDE A = the claim's top supporting experiments, SIDE B = the attack.
    # Claims with adjudicator pairs or disagreed replications go through
    # sources 1/2 first (those bases would keep the claim DISPUTED anyway).
    arows = conn.execute("""
        SELECT kc.id AS claim_id, kc.hypothesis_text,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'DISPUTED'
          AND COALESCE(kc.is_meta, 0) = 0
          AND COALESCE(kc.circular_construction, 0) = 0
          AND EXISTS (
              SELECT 1 FROM adversarial_replications ar
              WHERE ar.claim_id = kc.id AND ar.status = 'refuted')
          AND NOT EXISTS (
              SELECT 1 FROM answer_adjudications aa
              WHERE aa.claim_id = kc.id AND aa.consistent = 0
                AND aa.incompatibilities IS NOT NULL
                AND aa.incompatibilities != '[]')
          AND NOT EXISTS (
              SELECT 1 FROM replication_results rr
              WHERE rr.replication_status = 'disagreed'
                AND (rr.original_experiment_id = kc.first_experiment_id
                     OR rr.original_experiment_id = kc.last_experiment_id))
          AND NOT EXISTS (
              SELECT 1 FROM dispute_arbitrations da
              WHERE da.claim_id = kc.id
                AND da.status IN ('pending', 'resolved_a', 'resolved_b',
                                  'both_wrong', 'regime_split'))
        ORDER BY kc.weighted_support_count DESC
        LIMIT ?""", (limit,)).fetchall()

    for row in arows:
        atk = conn.execute("""
            SELECT ar.id, ar.experiment_id,
                   COALESCE(e.result, ar.notes, '') AS finding
            FROM adversarial_replications ar
            LEFT JOIN experiments e ON e.id = ar.experiment_id
            WHERE ar.claim_id = ? AND ar.status = 'refuted'
            ORDER BY ar.id DESC LIMIT 2""", (row["claim_id"],)).fetchall()
        atk = [a for a in atk if a["experiment_id"]]
        if not atk:
            continue
        supports = conn.execute("""
            SELECT ce.experiment_id,
                   COALESCE(NULLIF(TRIM(ce.key_finding), ''), e.result) AS finding
            FROM claim_evidence ce
            LEFT JOIN experiments e ON e.id = ce.experiment_id
            WHERE ce.claim_id = ?
              AND COALESCE(ce.evidence_type, 'support') = 'support'
            ORDER BY ce.confidence DESC, ce.id DESC LIMIT 3""",
            (row["claim_id"],)).fetchall()
        supports = [s for s in supports if s["finding"]]
        if not supports:
            continue
        atk_ids = {str(a["experiment_id"]) for a in atk}
        side_a = sorted({str(s["experiment_id"]) for s in supports} - atk_ids)
        side_b = sorted(atk_ids)
        if not side_a or not side_b:
            continue
        pairs = [{"a": side_a[0], "b": a["experiment_id"],
                  "reason": (f"claim support: {str(supports[0]['finding'] or '')[:140]}"
                             f" || attack: {str(a['finding'] or '')[:140]}")}
                 for a in atk]
        out.append({"source_kind": "adversarial", "row": row, "pairs": pairs,
                    "side_a": side_a, "side_b": side_b,
                    "rr_ids": None, "ar_ids": [a["id"] for a in atk]})

    # ── Source 4: false consensus (spurious agreement) — the SA settlement lane ──
    # SA >= 0.6 blocks ESTABLISHED but was the only gate with NO active
    # settlement path — a high-SA claim just sat blocked forever. Target the
    # claims where SA is the LIVE blocker: REPLICATED with a survived attack
    # (measured 24/28 survived-attack claims SA-blocked when this landed).
    # These claims are NOT DISPUTED — the supports agree on the flag while
    # diverging in substance. The decisive experiment pins the actual
    # direction/magnitude; A_CORRECT/B_CORRECT retracts the wrong side and
    # the 15m SA recompute (which now reads only surviving evidence) drops
    # the score; REGIME_SPLIT covers "both right in different regimes".
    srows = conn.execute("""
        SELECT kc.id AS claim_id, kc.hypothesis_text,
               COALESCE(kc.weighted_support_count, 0) AS wsc
        FROM knowledge_claims kc
        WHERE kc.claim_status = 'REPLICATED'
          AND COALESCE(kc.is_meta, 0) = 0
          AND COALESCE(kc.circular_construction, 0) = 0
          AND COALESCE(kc.spurious_agreement, 0) >= 0.6
          AND EXISTS (
              SELECT 1 FROM adversarial_replications ar
              WHERE ar.claim_id = kc.id AND ar.status = 'survived')
          AND NOT EXISTS (
              SELECT 1 FROM dispute_arbitrations da
              WHERE da.claim_id = kc.id
                AND da.status IN ('pending', 'resolved_a', 'resolved_b',
                                  'both_wrong', 'regime_split'))
        ORDER BY kc.weighted_support_count DESC
        LIMIT ?""", (limit,)).fetchall()

    for row in srows:
        supports = conn.execute("""
            SELECT ce.experiment_id,
                   COALESCE(NULLIF(TRIM(ce.key_finding), ''), e.result) AS finding
            FROM claim_evidence ce
            LEFT JOIN experiments e ON e.id = ce.experiment_id
            WHERE ce.claim_id = ?
              AND COALESCE(ce.evidence_type, 'support') = 'support'
            ORDER BY ce.confidence DESC, ce.id DESC LIMIT 8""",
            (row["claim_id"],)).fetchall()
        side_a, side_b, pairs = _split_sa_sides(supports)
        if not side_a or not side_b:
            continue
        out.append({"source_kind": "spurious_agreement", "row": row,
                    "pairs": pairs, "side_a": side_a, "side_b": side_b,
                    "rr_ids": None, "ar_ids": None})

    # Highest-value disputes get the free slots regardless of source.
    out.sort(key=lambda t: -(t["row"]["wsc"] or 0))
    return out[:min(slots, limit)]


def _findings_for(conn, claim_id, exp_ids):
    got = {}
    for eid in exp_ids:
        variants = tuple(_id_variants(eid))
        q = ",".join("?" * len(variants))
        r = conn.execute(f"""
            SELECT ce.experiment_id,
                   COALESCE(NULLIF(TRIM(ce.key_finding), ''), e.result) AS finding,
                   e.kanban_task_id
            FROM claim_evidence ce
            LEFT JOIN experiments e ON e.id = ce.experiment_id
            WHERE ce.claim_id = ? AND ce.experiment_id IN ({q})
            LIMIT 1""", (claim_id, *variants)).fetchone()
        if r and r["finding"]:
            got[eid] = {"finding": str(r["finding"])[:FINDING_CHARS],
                        "kanban_task_id": r["kanban_task_id"]}
    return got


def _findings_for_pairs(conn, pairs, side_a, side_b):
    """Findings carried verbatim in the pair reasons (replication and
    adversarial sources), kanban_task_id from experiments for artifact lookup."""
    got = {}
    for p in pairs:
        for key, side in (("a", side_a), ("b", side_b)):
            eid = str(p[key])
            if eid not in side or eid in got:
                continue
            finding = (p.get("reason") or "").split(" || ")[0 if key == "a" else -1]
            finding = re.sub(r"^(original|retest|claim support|attack):\s*", "", finding)
            r = conn.execute(
                "SELECT result, kanban_task_id FROM experiments WHERE id = ?",
                (eid,)).fetchone()
            got[eid] = {
                "finding": (finding or (r["result"] if r else "") or
                            "(finding text unavailable)")[:FINDING_CHARS],
                "kanban_task_id": r["kanban_task_id"] if r else None,
            }
    return got


# pre-triage: separate disputes a single recomputation settles from those that
# need open-ended judgment. Conditional/interpretive language means the sides
# likely measured different things (different sample, regime, definition) — a
# blind recompute would miss the point, so those keep the decisive-experiment
# card. A bare quantity disagreement (same thing, two numbers) is cheaper and
# more decisive to just re-derive.
_NUM_RE = re.compile(r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')
_COND_RE = re.compile(
    r'\b(regime|sample size|different\s+(sample|seed|data|dataset|condition|'
    r'setup|construction|definition|metric|weighting|model)|not\s+'
    r'(statistically\s+)?significant|depends\s+on|conditional|context[- ]dependent|'
    r'only\s+when|under\s+(different|certain)|qualitativ|interpret|mechanism\s+differ)\b',
    re.I)


def classify_recomputable_dispute(pairs, findings, side_a, side_b):
    """Return (is_recomputable, hint). Recomputable = the two sides report
    DIFFERENT numeric values for what reads as the SAME quantity, with little
    conditional/interpretive language. Conservative: when in doubt it returns
    False and the dispute takes the normal (interpretive) arbitration card.
    Safe by construction anyway — the recompute card keeps the full verdict
    contract (incl. REGIME_SPLIT/BOTH_WRONG), so a misroute self-corrects."""
    reason_text = ' '.join((p.get('reason') or '') for p in pairs)
    if not reason_text.strip():
        reason_text = ' '.join(
            (findings.get(e, {}).get('finding') or '') for e in (side_a + side_b))
    numeric_pairs, hint = 0, None
    sources = ([p.get('reason') or '' for p in pairs]
               or [findings.get(e, {}).get('finding') or '' for e in (side_a + side_b)])
    for r in sources:
        nums = [float(x) for x in _NUM_RE.findall(r)]
        if len(nums) >= 2:
            lo, hi = min(nums), max(nums)
            if max(abs(hi), abs(lo)) > 1e-9 and abs(hi - lo) / max(abs(hi), abs(lo)) > 0.05:
                numeric_pairs += 1
                if hint is None:
                    hint = r[:180]
    cond_hits = len(_COND_RE.findall(reason_text))
    total = max(len(sources), 1)
    is_recomputable = (numeric_pairs >= 1
                       and numeric_pairs >= total * 0.5      # numeric disagreement dominates
                       and cond_hits == 0)                   # NO conditional/interpretive language
    return is_recomputable, hint


def make_body(claim, pairs, side_a, side_b, findings, code_paths,
              source_kind="adjudication", recompute=False, recompute_hint=None):
    pair_lines = "\n".join(
        f"- {p['a']}  vs  {p['b']}: {str(p.get('reason', ''))[:300]}"
        for p in pairs[:4])

    def side_block(label, ids):
        lines = []
        for eid in ids:
            f = findings.get(eid, {}).get("finding", "(finding text unavailable)")
            lines.append(f"  [{eid}] {f}")
        return f"SIDE {label} ({', '.join(ids)}):\n" + "\n".join(lines)

    code_lines = ("\n".join(f"- {p}" for p in code_paths)
                  if code_paths else "- (none preserved — reconstruct from the findings)")
    hyp = (claim["hypothesis_text"] or "").strip()

    if source_kind == "replication":
        context = """CONTEXT: This claim is DISPUTED because an independent retest DISAGREED with the
original experiment. SIDE A is the original experiment; SIDE B is the retest that
contradicted it. Your job is NOT to run the claim a third time blindly — it is to
build the ONE experiment whose outcome the original and the retest PREDICT
DIFFERENTLY (find the crux of their disagreement first), run it, and report which
side survives."""
        contra_header = "THE DISAGREEMENT (original vs retest, findings verbatim):"
    elif source_kind == "adversarial":
        context = """CONTEXT: This claim is DISPUTED because an adversarial attack reported BREAKING it.
SIDE A is the claim's supporting evidence; SIDE B is the attack. Your job is to judge
whether the break is GENUINE: did the attack test the actual claim or a strawman? Does
the break reproduce under an independent construction? Build the ONE experiment that
discriminates — if the attack's break holds, B is correct; if the claim's mechanism
survives a faithful version of the attack, A is correct."""
        contra_header = "THE BREAK (claim support vs attack, findings verbatim):"
    elif source_kind == "spurious_agreement":
        context = """CONTEXT: This claim is NOT disputed — every support voted CONFIRMED — but the supports
DISAGREE IN SUBSTANCE (direction, magnitude, or their text contradicts their vote), which
blocks promotion as false consensus. SIDE A is the majority/consistent reading; SIDE B is
the divergent one. Your job is to pin down the ACTUAL direction and magnitude with one
decisive measurement: state what A and B each predict, run it, report which substance is
right. If both are right under different conditions, that is REGIME_SPLIT."""
        contra_header = "THE FALSE CONSENSUS (supports agreeing on the flag, not the substance):"
    else:
        context = """CONTEXT: This claim is DISPUTED because its supporting experiments contradict each other
at the answer level (found by automated adjudication). Your job is NOT to re-test the
claim from scratch — it is to build the ONE experiment whose outcome the two sides
PREDICT DIFFERENTLY, run it, and report which side survives."""
        contra_header = "THE CONTRADICTION (adjudicator-quoted):"

    if recompute:
        # the recomputable subset: the crux is already a named quantity with two
        # differing values. Skip crux-hunting and open-ended experiment design —
        # re-derive the number directly. Same verdict contract, so if the numbers
        # turn out to come from genuinely different constructions, REGIME_SPLIT.
        header = ("DISPUTE ARBITRATION (RECOMPUTE) — the two sides report DIFFERENT "
                  "VALUES for what should be the SAME quantity. Re-derive it directly "
                  "and report which side's number is correct.")
        hint_line = (f"\nDISPUTED QUANTITY (from the adjudicator): {recompute_hint}\n"
                     if recompute_hint else "\n")
        method = f"""METHOD (recompute, do NOT design an open-ended experiment):
1. Name the exact disputed quantity and both reported values.{hint_line}\
2. Re-derive it from scratch with real code on the SHARED construction both sides
   describe — exact arithmetic where the quantity is analytic; a clean minimal
   simulation (fixed + varied seeds) where it is empirical. State the number you get.
3. Compare to A and B. If your value matches one side, the other is refuted.
4. GUARD against a false recompute: if A and B actually measured the quantity under
   different constructions/definitions/regimes (so BOTH numbers are locally correct),
   that is NOT a wrong answer to settle — report REGIME_SPLIT and state the boundary."""
    else:
        header = ("DISPUTE ARBITRATION — two sides assert INCOMPATIBLE answers to the "
                  "same question.\nDesign a decisive experiment that discriminates between them.")
        method = """METHOD:
1. Identify the crux: the specific quantity/direction/functional form the sides disagree on.
2. Design the discriminating test — a parameter regime or measurement where side A and
   side B predict clearly different outcomes. State both predictions BEFORE running.
3. Run it with real code. Vary seeds; check the result is outside noise.
4. Consider the regimes: if each side is right under different conditions, find the boundary."""

    return f"""{header}

{context}

HYPOTHESIS: {hyp}

{contra_header}
{pair_lines}

{side_block('A', side_a)}

{side_block('B', side_b)}

ORIGINAL EXPERIMENT CODE (preserved artifacts — read before designing):
{code_lines}

{method}

EXPECTED — your finding MUST contain exactly one of these verdict lines:
  ARBITRATION_VERDICT: A_CORRECT      (side A's answer survives; B's refuted by the test)
  ARBITRATION_VERDICT: B_CORRECT      (mirror)
  ARBITRATION_VERDICT: BOTH_WRONG     (the test refutes both answers)
  ARBITRATION_VERDICT: REGIME_SPLIT   (each right in a different regime — STATE THE
                                       BOUNDARY and submit one --queue question per regime)

DISPUTE_ARBITRATION_FOR_CLAIM: {claim['claim_id']}

RESULT WRITING (MANDATORY — run BEFORE kanban_complete):
  python3 ~/.hermes/scripts/write_worker_result.py \\
    --experiment {{exp_id}} \\
    --finding "ARBITRATION_VERDICT: <verdict>. WHAT the discriminating test was, both sides' predictions, what happened. DISPUTE_ARBITRATION_FOR_CLAIM: {claim['claim_id']}" \\
    --supported --confidence 0.XX --domain auto \\
    --tags DISPUTE_ARBITRATION --basis independent_computation \\
    --files "{{exp_id}}.py,{{exp_id}}_results.json"
(use --supported if either side or a regime split survived, --refuted for BOTH_WRONG;
keep both marker lines in the finding so intake can route the outcome)
"""


def resolve_arbitration(conn, claim_id, verdict_token, exp_id, finding, now=None):
    """Apply an arbitration outcome. Called from apply_worker_results intake.

    Returns the resolution status string, or None if no pending arbitration
    matched. Never raises (caller wraps anyway)."""
    now = now or time.time()
    arb = conn.execute(
        "SELECT * FROM dispute_arbitrations WHERE claim_id = ? AND status = 'pending' "
        "ORDER BY id DESC LIMIT 1", (claim_id,)).fetchone()
    if not arb:
        return None

    token = (verdict_token or "").upper()
    status_map = {"A_CORRECT": "resolved_a", "B_CORRECT": "resolved_b",
                  "BOTH_WRONG": "both_wrong", "REGIME_SPLIT": "regime_split"}
    status = status_map.get(token)
    if status is None:
        # completed task but unusable verdict — expire so the claim can retry
        conn.execute(
            "UPDATE dispute_arbitrations SET status='expired', resolved_at=?, "
            "experiment_id=?, notes=? WHERE id=?",
            (now, exp_id, f"unparseable verdict: {(finding or '')[:150]}", arb["id"]))
        return "expired"

    # Retract losing evidence so wsc/promotion queries stop counting it.
    # Defensive: never retract an id that also appears on the winning side
    # (sides are disjoint by construction since the overlap fix, but old
    # rows may predate it).
    side_a = json.loads(arb["side_a"] or "[]")
    side_b = json.loads(arb["side_b"] or "[]")
    losers = []
    if status == "resolved_a":
        losers = [e for e in side_b if e not in side_a]
    elif status == "resolved_b":
        losers = [e for e in side_a if e not in side_b]
    elif status == "both_wrong":
        losers = side_a + side_b
    retracted = 0
    if losers:
        variants = set()
        for eid in losers:
            variants |= _id_variants(eid)
        q = ",".join("?" * len(variants))
        cur = conn.execute(
            f"UPDATE claim_evidence SET evidence_type = 'retracted_by_arbitration' "
            f"WHERE claim_id = ? AND experiment_id IN ({q}) "
            f"AND COALESCE(evidence_type, 'support') = 'support'",
            (claim_id, *sorted(variants)))
        retracted = cur.rowcount

    # Replication-sourced arbitrations settle the underlying disagreement
    # rows: maturity's contradiction recompute counts
    # replication_status='disagreed', so rewriting the status is what clears
    # the dispute (and stops detect_replication_contradictions re-adding it).
    # The rows keep their validation_finding, so they still count as
    # independent retests. Expired arbitrations never reach here — an
    # unusable verdict leaves the rows 'disagreed' and the claim retryable.
    _keys = arb.keys()
    if ("source_kind" in _keys and arb["source_kind"] == "replication"
            and "rr_ids" in _keys and arb["rr_ids"]):
        try:
            _rr_ids = [int(x) for x in json.loads(arb["rr_ids"])]
        except (json.JSONDecodeError, TypeError, ValueError):
            _rr_ids = []
        if _rr_ids:
            q = ",".join("?" * len(_rr_ids))
            conn.execute(
                f"UPDATE replication_results "
                f"SET replication_status = 'disagreed_arbitrated' "
                f"WHERE id IN ({q}) AND replication_status = 'disagreed'",
                _rr_ids)

    # Adversarial-sourced arbitrations settle the attack rows the same way:
    # the contradiction recompute counts ar.status='refuted', so the settled
    # status clears the dispute. A settled row also stops blocking the attack
    # enqueuer's one-per-claim dedup — an A_CORRECT claim returns to
    # REPLICATED still needing a genuine 'survived' row for ESTABLISHED, so
    # it gets re-attacked rather than credited by the arbitration.
    if ("source_kind" in _keys and arb["source_kind"] == "adversarial"
            and "ar_ids" in _keys and arb["ar_ids"]):
        try:
            _ar_ids = [int(x) for x in json.loads(arb["ar_ids"])]
        except (json.JSONDecodeError, TypeError, ValueError):
            _ar_ids = []
        if _ar_ids:
            q = ",".join("?" * len(_ar_ids))
            conn.execute(
                f"UPDATE adversarial_replications "
                f"SET status = 'refuted_arbitrated' "
                f"WHERE id IN ({q}) AND status = 'refuted'",
                _ar_ids)

    # Clear this dispute's contribution to contradiction_count (the adjudicator
    # bumped min(n_pairs, 3) — see apply_contradiction). Other disputes'
    # contributions remain; at 0 the maturity recompute lifts DISPUTED.
    # (Since contradiction_count became a recomputed signal this decrement is
    # immediate marking only — the next detector cycle derives the canonical
    # value from the settled bases.)
    bump = min(arb["n_pairs"] or 1, 3)
    conn.execute(
        "UPDATE knowledge_claims SET contradiction_count = "
        "MAX(0, COALESCE(contradiction_count, 0) - ?), last_updated_at = ? "
        "WHERE id = ?",
        (bump, time.time(), claim_id))   # epoch float — last_updated_at is TEXT-affinity, ISO strings break CAST(... AS REAL)

    conn.execute(
        "UPDATE dispute_arbitrations SET status=?, resolved_at=?, experiment_id=?, "
        "notes=? WHERE id=?",
        (status, now, exp_id,
         f"retracted {retracted} evidence rows; {(finding or '')[:150]}", arb["id"]))
    return status


def main():
    ap = argparse.ArgumentParser(description="Enqueue decisive experiments for DISPUTED claims")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=MAX_PENDING)
    args = ap.parse_args()

    conn = get_db()
    ensure_table(conn)
    expire_stale(conn, args.dry_run)
    targets = get_targets(conn, args.limit)
    if not targets:
        print("No DISPUTED claims awaiting arbitration.")
        conn.close()
        return 0

    hermes = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    created = 0
    for t in targets:
        row = t["row"]
        src = t.get("source_kind", "adjudication")
        claim = {"claim_id": row["claim_id"], "hypothesis_text": row["hypothesis_text"]}
        all_ids = t["side_a"] + t["side_b"]
        if src in ("replication", "adversarial"):
            findings = _findings_for_pairs(conn, t["pairs"],
                                           t["side_a"], t["side_b"])
        else:
            findings = _findings_for(conn, row["claim_id"], all_ids)
        code_paths = []
        for eid in all_ids:
            ktid = findings.get(eid, {}).get("kanban_task_id")
            p, _ = find_archived_code(hermes, eid, ktid)
            if p:
                code_paths.append(p)

        recompute, rc_hint = classify_recomputable_dispute(
            t["pairs"], findings, t["side_a"], t["side_b"])
        triage = "recompute" if recompute else "arbitrate"

        exp_id = f"exp_arb_{row['claim_id']}_{time.strftime('%y%m%d%H%M', time.gmtime())}"
        body = make_body(claim, t["pairs"], t["side_a"], t["side_b"],
                         findings, code_paths, source_kind=src,
                         recompute=recompute, recompute_hint=rc_hint
                         ).replace("{exp_id}", exp_id)
        tag = "[RECOMPUTE]" if recompute else "[ARBITRATION]"
        title = (f"{exp_id}: {tag} Settle claim #{row['claim_id']}: "
                 f"{(row['hypothesis_text'] or '')[:70]}")

        if args.dry_run:
            print(f"[DRY-RUN] would {triage} claim {row['claim_id']} [{src}] "
                  f"(wsc={row['wsc']:.1f}, pairs={len(t['pairs'])}, "
                  f"A={t['side_a']}, B={t['side_b']}, code={len(code_paths)})")
            continue

        r = subprocess.run(
            [sys.executable, SAFE_CREATE, title, "--assignee", "default",
             # p4: above the retest flood — attacks/arbitration are the ladder's top
             # and must not starve behind ~1k p3 retest tasks.
             "--priority", "4", "--body", body],
            capture_output=True, text=True, timeout=60)
        m = re.search(r"t_[a-f0-9]+", r.stdout or "")
        if r.returncode == 0 and m:
            # Free-coder slice for recompute cards (see RECOMPUTE_CODER note).
            # Seed includes the claim's prior arbitration count so a retry
            # after an expired free attempt falls back to the fleet default.
            override = None
            if recompute:
                attempts = conn.execute(
                    "SELECT COUNT(*) AS c FROM dispute_arbitrations "
                    "WHERE claim_id = ?", (row["claim_id"],)).fetchone()["c"]
                seed = int(hashlib.md5(
                    f"rc:{row['claim_id']}:{attempts}".encode()).hexdigest()[:8], 16)
                if RECOMPUTE_CODER and seed % RECOMPUTE_CODER_MOD == 0:
                    if set_model_override(m.group(0), RECOMPUTE_CODER):
                        override = RECOMPUTE_CODER
            conn.execute("""
                INSERT INTO dispute_arbitrations
                (claim_id, adjudication_id, kanban_task_id, experiment_id,
                 side_a, side_b, n_pairs, created_at, status,
                 source_kind, rr_ids, ar_ids, triage, model_override)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (row["claim_id"],
                 row["adjudication_id"] if src == "adjudication" else None,
                 m.group(0), exp_id,
                 json.dumps(t["side_a"]), json.dumps(t["side_b"]),
                 len(t["pairs"]), time.time(), src,
                 json.dumps(t["rr_ids"]) if t.get("rr_ids") else None,
                 json.dumps(t["ar_ids"]) if t.get("ar_ids") else None,
                 triage, override))
            conn.commit()
            created += 1
            print(f"ENQUEUED {triage} for claim {row['claim_id']} "
                  f"[{src}] -> {m.group(0)} ({exp_id})"
                  + (f" model={override}" if override else ""))
        else:
            print(f"  create failed for claim {row['claim_id']}: rc={r.returncode} "
                  f"{(r.stdout or '')[:200]} {(r.stderr or '')[:150]}")

    print(f"\nEnqueued {created} arbitration(s)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
