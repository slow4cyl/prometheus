import os
#!/usr/bin/env python3
"""spurious_agreement.py - per-claim "spurious agreement" score for Prometheus.

WHY THIS EXISTS
---------------
Prometheus counts a claim's support via the structured hypothesis_supported flag
(1 = supported). The posterior / claim_status machinery reads ONLY that flag. But
workers routinely (a) write "REFUTED" in key_finding while selecting hs=1, and
(b) all select hs=1 while describing mutually incompatible magnitudes or directions
in their free text. The flag says "6 unanimous supports"; the text says "6 workers
found completely different things." Nothing in the system measures that gap.

This script computes, for every claim with >=2 evidence rows, a spurious_agreement
score in [0,1]: HIGH = the supports look unanimous by flag but disagree in substance
(the posterior is untrustworthy); LOW = the supports genuinely agree.

It is READ-MOSTLY: it only writes a new column (knowledge_claims.spurious_agreement
+ component columns) and never touches posterior, claim_status, support_count, or any
existing decision field. It cannot change system behavior on its own - it produces a
diagnostic signal a human (or a future routing rule) can act on.

COMPONENTS (each in [0,1], combined as a weighted max-ish blend; see compute_score)
  1. flag_text_mismatch_rate
       fraction of evidence rows whose key_finding verdict word (REFUTED/CONFIRMED/
       SUPPORTED/NO_TRANSFER/...) contradicts the hypothesis_supported flag.
  2. direction_disagreement
       among the SUPPORTS (hs=1), Gini-style split across {up, down, none} direction
       words found in the finding text. 0 = all same direction, ~1 = even split.
       KNOWN BLIND SPOT: this is a coarse keyword counter. For *comparative* claims
       ("A is higher than B"), both "A higher" and "B higher" contain "higher" and
       get labeled "up", so genuine which-way-does-it-go disagreement is missed.
       It catches up-vs-down and effect-vs-no-effect, not subject-flip. The
       magnitude_spread component is the reliable signal for comparative claims;
       direction_disagreement is a supplementary signal, not the primary one.
  3. magnitude_spread
       log10 range of numeric effect sizes (x-multipliers and %) extracted from the
       SUPPORTS, squashed to [0,1]. Large spread = supports that "agree" describe
       effects orders of magnitude apart. Multipliers > 20 are dropped as almost
       certainly not effect-size ratios (sample counts, misparsed percentages).
  4. non_directional
       1.0 if claim_type = NON_DIRECTIONAL (binary support is meaningless by design),
       else 0.0. Used as a soft prior, not a hard gate.

USAGE
  python3 spurious_agreement.py            # compute + write for all multi-evidence claims
  python3 spurious_agreement.py --dry-run  # compute + print summary, write nothing
  python3 spurious_agreement.py --top 20   # after a run, print the worst offenders
  python3 spurious_agreement.py --claim 3902   # explain one claim's score

SAFETY: WAL, busy_timeout, synchronous=NORMAL, chunked BEGIN IMMEDIATE commits.
Adds columns if missing (idempotent). Never deletes or modifies existing columns.
"""
import argparse
import math
import re
import sqlite3
import sys
import time

DB = os.path.expanduser("~/.hermes/prometheus.db")
CHUNK = 1000
MIN_EVIDENCE = 2  # claims with <2 evidence rows can't disagree with themselves

# --- verdict-word detection in key_finding free text --------------------------
# Ordered: first match wins. Patterns are anchored near the start of the finding.
_REFUTE_RE = re.compile(
    r"\b(REFUTED|NOT\s+CONFIRMED|NO[_\s]TRANSFER|DISPROVEN|FALSIFIED|"
    r"BROKE\s+THE\s+CLAIM|FAILED\s+TO|DOES\s+NOT|NOT\s+ACHIEVABLE|"
    r"ADVERSARIAL\s+(ATTACK\s+SUCCESSFUL|REPLICATION\s+REFUTED))", re.I)
_CONFIRM_RE = re.compile(
    r"\b(CONFIRMED|SUPPORTED|VALIDATED|REPLICATED|HOLDS|PROVEN|SUCCEEDS?)\b", re.I)
_PARTIAL_RE = re.compile(r"\b(PARTIAL(LY)?[_\s]?(CONFIRMED|SUPPORTED|REFUTED)?)\b", re.I)

# direction words among supports
_UP_RE = re.compile(r"\b(higher|increase[sd]?|positive|greater|more|rises?|grows?|"
                    r"stronger|improve[sd]?|wins?|exceeds?|above)\b", re.I)
_DOWN_RE = re.compile(r"\b(lower|decrease[sd]?|negative|less|fewer|drops?|falls?|"
                      r"weaker|degrade[sd]?|plateaus?|below|worse)\b", re.I)
_NONE_RE = re.compile(r"\b(no\s+(effect|difference|change|transfer|impact)|"
                      r"negligible|unchanged|independent\s+of|not\s+significant|"
                      r"no\s+correlation)\b", re.I)

# numeric magnitude tokens. We extract ONLY scale-comparable ratios:
#   - explicit multipliers: 2.7x, 1.22x  (already ratios)
#   - percentages:          95.2%        (converted to a 1+pct/100 ratio)
# We deliberately do NOT mix in raw F1/AUC/correlation/entropy values: those live
# on different, non-comparable scales (an F1 of 0.91 and a 2.7x ratio are not the
# same kind of number), so blending them inflates the log-range with noise. The
# multiplier+percent set is the cleanest cross-finding "how big is the effect"
# signal available in free text.
_NUM_X_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[x\u00d7]\b", re.I)        # multipliers
_NUM_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")                       # percentages


def verdict_of(text):
    """Return 'refute' | 'confirm' | 'partial' | None from finding free text."""
    if not text:
        return None
    head = text[:200]
    if _REFUTE_RE.search(head):
        return "refute"
    if _PARTIAL_RE.search(head):
        return "partial"
    if _CONFIRM_RE.search(head):
        return "confirm"
    return None


def direction_of(text):
    """Coarse direction label from a support's finding text."""
    if not text:
        return None
    t = text[:300]
    if _NONE_RE.search(t):
        return "none"
    up = len(_UP_RE.findall(t))
    down = len(_DOWN_RE.findall(t))
    if up == 0 and down == 0:
        return None
    if up > down:
        return "up"
    if down > up:
        return "down"
    return "mixed"


def magnitudes_of(text):
    """Extract comparable numeric effect sizes from a support's finding text."""
    if not text:
        return []
    t = text[:400]
    vals = []
    for m in _NUM_X_RE.findall(t):
        v = float(m)
        if 0 < v <= 20:               # drop sample counts / misparsed % (>20x is not an effect ratio)
            vals.append(v)            # multiplier, already a ratio
    for m in _NUM_PCT_RE.findall(t):
        v = float(m)
        if 0 < v <= 100:
            vals.append(1.0 + v / 100.0)  # treat % as a relative magnitude
    return vals


def gini_split(labels):
    """1 - sum(p_i^2) over label frequencies. 0 = unanimous, ->1 = even split."""
    labels = [x for x in labels if x is not None]
    if len(labels) < 2:
        return 0.0
    from collections import Counter
    n = len(labels)
    return 1.0 - sum((cnt / n) ** 2 for cnt in Counter(labels).values())


def compute_score(rows, claim_type):
    """rows: list of (hypothesis_supported, key_finding). Return (score, components)."""
    n = len(rows)
    # 1. flag/text mismatch
    mism = 0
    counted = 0
    for hs, kf in rows:
        v = verdict_of(kf)
        if v is None or hs is None:
            continue
        counted += 1
        if (v == "refute" and hs == 1) or (v == "confirm" and hs == 0):
            mism += 1
    flag_text_mismatch_rate = (mism / counted) if counted else 0.0

    # among supports only (hs=1) for direction & magnitude
    sup_findings = [kf for hs, kf in rows if hs == 1]
    # 2. direction disagreement
    dirs = [direction_of(kf) for kf in sup_findings]
    direction_disagreement = gini_split(dirs)

    # 3. magnitude spread (log10 range over all magnitudes found in supports)
    mags = []
    for kf in sup_findings:
        mags.extend(magnitudes_of(kf))
    if len(mags) >= 2:
        lo, hi = min(mags), max(mags)
        log_range = math.log10(hi / lo) if lo > 0 else 0.0
        magnitude_spread = min(log_range / 2.0, 1.0)  # 2 orders of magnitude -> 1.0
    else:
        magnitude_spread = 0.0

    # 4. non-directional prior
    non_directional = 1.0 if (claim_type == "NON_DIRECTIONAL") else 0.0

    # --- combine -------------------------------------------------------------
    # The first three are independent evidence of substantive disagreement; take
    # a soft-OR (probabilistic union) so any one strong signal lifts the score.
    parts = [flag_text_mismatch_rate, direction_disagreement, magnitude_spread]
    soft_or = 1.0
    for p in parts:
        soft_or *= (1.0 - max(0.0, min(1.0, p)))
    soft_or = 1.0 - soft_or
    # non_directional adds a fixed prior bump (capped at 1.0): even with no text
    # signal, a non-directional claim's "supports" are structurally unreliable.
    score = min(1.0, soft_or + 0.25 * non_directional)

    return round(score, 4), {
        "n_evidence": n,
        "flag_text_mismatch_rate": round(flag_text_mismatch_rate, 4),
        "direction_disagreement": round(direction_disagreement, 4),
        "magnitude_spread": round(magnitude_spread, 4),
        "non_directional": non_directional,
    }


def ensure_columns(cur):
    cols = {r[1] for r in cur.execute("PRAGMA table_info(knowledge_claims)").fetchall()}
    add = []
    if "spurious_agreement" not in cols:
        add.append("ALTER TABLE knowledge_claims ADD COLUMN spurious_agreement REAL")
    if "sa_flag_mismatch" not in cols:
        add.append("ALTER TABLE knowledge_claims ADD COLUMN sa_flag_mismatch REAL")
    if "sa_direction_disagree" not in cols:
        add.append("ALTER TABLE knowledge_claims ADD COLUMN sa_direction_disagree REAL")
    if "sa_magnitude_spread" not in cols:
        add.append("ALTER TABLE knowledge_claims ADD COLUMN sa_magnitude_spread REAL")
    if "sa_computed_at" not in cols:
        add.append("ALTER TABLE knowledge_claims ADD COLUMN sa_computed_at REAL")
    for stmt in add:
        cur.execute(stmt)
    return bool(add)


def fetch_claim_rows(cur, claim_id):
    # Retracted evidence (evidence_type='retracted_by_arbitration') is excluded
    # — the same rule wsc and every promotion-band query follow. Without this
    # filter an arbitration that retracts the divergent supports can never
    # lower the SA score, so the SA gate would stay shut on settled claims
    # (this was the case until the SA settlement lane landed).
    return cur.execute("""
        SELECT DISTINCT wr.hypothesis_supported, wr.key_finding
        FROM claim_evidence ce JOIN worker_results wr ON wr.id = ce.worker_result_id
        WHERE ce.claim_id = ? AND wr.key_finding IS NOT NULL
          AND COALESCE(ce.evidence_type, 'support') = 'support'
    """, (claim_id,)).fetchall()


def compute_and_write(conn, commit=True):
    """Recompute spurious_agreement for all multi-evidence claims on an EXISTING
    connection. Designed to be called from contradiction_detector.run() so the
    score stays in sync with the 5-minute claim-lifecycle cycle. Returns the count
    of claims scored at >= 0.6 (likely false consensus). Never raises into the
    caller's cycle — on any error it rolls back its own work and returns -1."""
    try:
        cur = conn.cursor()
        ensure_columns(cur)
        claim_ids = [r[0] for r in cur.execute("""
            SELECT ce.claim_id
            FROM claim_evidence ce JOIN worker_results wr ON wr.id = ce.worker_result_id
            WHERE wr.key_finding IS NOT NULL
              AND COALESCE(ce.evidence_type, 'support') = 'support'
            GROUP BY ce.claim_id HAVING COUNT(DISTINCT wr.id) >= ?
        """, (MIN_EVIDENCE,)).fetchall()]
        ctype = dict(cur.execute("SELECT id, claim_type FROM knowledge_claims").fetchall())
        now = time.time()
        high = 0
        updates = []
        for cid in claim_ids:
            rows = fetch_claim_rows(cur, cid)
            score, comp = compute_score(rows, ctype.get(cid))
            if score >= 0.6:
                high += 1
            updates.append((score, comp["flag_text_mismatch_rate"],
                            comp["direction_disagreement"], comp["magnitude_spread"],
                            now, cid))
        cur.executemany("""UPDATE knowledge_claims
            SET spurious_agreement=?, sa_flag_mismatch=?, sa_direction_disagree=?,
                sa_magnitude_spread=?, sa_computed_at=? WHERE id=?""", updates)
        if commit:
            conn.commit()
        return high
    except Exception as e:  # never break the host cycle
        try:
            conn.rollback()
        except Exception:
            pass
        sys.stderr.write(f"spurious_agreement.compute_and_write failed: {e}\n")
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=0, help="print N worst claims (post-run read)")
    ap.add_argument("--impactful", action="store_true",
                    help="with --top: restrict to claims where false consensus matters "
                         "(>=3 distinct supports AND posterior>=0.7) - the actionable set")
    ap.add_argument("--claim", type=int, default=0, help="explain one claim's score")
    args = ap.parse_args()

    if args.top:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        con.execute("PRAGMA busy_timeout=20000")
        where = "spurious_agreement IS NOT NULL"
        if args.impactful:
            # distinct supports via claim_evidence (support_count is the stale field)
            where += """ AND posterior >= 0.7 AND id IN (
                SELECT ce.claim_id FROM claim_evidence ce
                JOIN worker_results wr ON wr.id = ce.worker_result_id
                WHERE wr.hypothesis_supported = 1
                GROUP BY ce.claim_id HAVING COUNT(DISTINCT wr.id) >= 3)"""
        rows = con.execute(f"""
            SELECT id, substr(hypothesis_text,1,70), claim_status, claim_type,
                   support_count, posterior, spurious_agreement,
                   sa_flag_mismatch, sa_direction_disagree, sa_magnitude_spread
            FROM knowledge_claims
            WHERE {where}
            ORDER BY spurious_agreement DESC, posterior DESC LIMIT ?
        """, (args.top,)).fetchall()
        con.close()
        print(f"{'id':>7} {'SA':>5} {'flag':>5} {'dir':>5} {'mag':>5} {'stat':<11} {'sup':>4} {'post':>5}  text")
        for r in rows:
            print(f"{r[0]:>7} {r[6]:>5.2f} {r[7]:>5.2f} {r[8]:>5.2f} {r[9]:>5.2f} "
                  f"{r[2] or '':<11} {r[4] or 0:>4} {r[5] or 0:>5.2f}  {r[1]}")
        return 0

    if args.claim:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
        con.execute("PRAGMA busy_timeout=20000")
        cur = con.cursor()
        ct = cur.execute("SELECT claim_type, claim_status, posterior, support_count FROM knowledge_claims WHERE id=?",
                         (args.claim,)).fetchone()
        rows = fetch_claim_rows(cur, args.claim)
        score, comp = compute_score(rows, ct[0] if ct else None)
        print(f"claim #{args.claim}  type={ct[0]} status={ct[1]} posterior={ct[2]} support_count={ct[3]}")
        print(f"spurious_agreement = {score}")
        print(f"components = {comp}")
        print("\nevidence (verdict | direction | magnitudes | hs):")
        for hs, kf in rows:
            print(f"  hs={hs} v={verdict_of(kf)!s:8} dir={direction_of(kf)!s:6} "
                  f"mags={magnitudes_of(kf)} :: {(kf or '')[:90]}")
        con.close()
        return 0

    # full compute
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA synchronous=NORMAL")
    cur = con.cursor()

    if not args.dry_run:
        if ensure_columns(cur):
            con.commit()
            print("added spurious_agreement columns to knowledge_claims")

    claim_ids = [r[0] for r in cur.execute("""
        SELECT ce.claim_id
        FROM claim_evidence ce JOIN worker_results wr ON wr.id = ce.worker_result_id
        WHERE wr.key_finding IS NOT NULL
        GROUP BY ce.claim_id HAVING COUNT(DISTINCT wr.id) >= ?
    """, (MIN_EVIDENCE,)).fetchall()]
    total = len(claim_ids)
    print(f"claims with >={MIN_EVIDENCE} evidence rows: {total}")

    ctype = dict(cur.execute("SELECT id, claim_type FROM knowledge_claims").fetchall())

    t0 = time.time()
    written = 0
    high = 0
    dist = {"0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
    updates = []
    for cid in claim_ids:
        rows = fetch_claim_rows(cur, cid)
        score, comp = compute_score(rows, ctype.get(cid))
        if score < 0.2:
            dist["0-0.2"] += 1
        elif score < 0.4:
            dist["0.2-0.4"] += 1
        elif score < 0.6:
            dist["0.4-0.6"] += 1
        elif score < 0.8:
            dist["0.6-0.8"] += 1
        else:
            dist["0.8-1.0"] += 1
        if score >= 0.6:
            high += 1
        updates.append((score, comp["flag_text_mismatch_rate"],
                        comp["direction_disagreement"], comp["magnitude_spread"],
                        time.time(), cid))
        if not args.dry_run and len(updates) >= CHUNK:
            cur.execute("BEGIN IMMEDIATE")
            cur.executemany("""UPDATE knowledge_claims
                SET spurious_agreement=?, sa_flag_mismatch=?, sa_direction_disagree=?,
                    sa_magnitude_spread=?, sa_computed_at=? WHERE id=?""", updates)
            con.commit()
            written += len(updates)
            updates = []

    if not args.dry_run and updates:
        cur.execute("BEGIN IMMEDIATE")
        cur.executemany("""UPDATE knowledge_claims
            SET spurious_agreement=?, sa_flag_mismatch=?, sa_direction_disagree=?,
                sa_magnitude_spread=?, sa_computed_at=? WHERE id=?""", updates)
        con.commit()
        written += len(updates)

    con.close()
    mode = "DRY-RUN (no writes)" if args.dry_run else f"wrote {written} claims"
    print(f"DONE [{mode}] in {time.time()-t0:.1f}s")
    print(f"score distribution: {dist}")
    print(f"claims with spurious_agreement >= 0.6 (likely false consensus): {high}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
