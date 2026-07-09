#!/usr/bin/env python3
"""empirical_fact_classifier.py — separate LOOKUP FACTS from DISCOVERIES.

Why (2026-07-04): the discovery shelf carries a class of claims that are not
discoveries at all — verifications of named real-world EVENTS the workers
looked up: "GW250114, detected by LIGO on 2025-01-14, was the loudest GW ever"
(#56817, ESTABLISHED), "NASA launched SPHEREx in March 2025" (#57076,
ESTABLISHED), "the 2025 Nobel Prize in Chemistry went to...", "the EMA approved
67 novel drugs in 2025". These answer "did event E happen / what were its
parameters", not "what mechanism generalizes". They inflate ESTABLISHED with
fact-checking, and (worse) a WRONG lookup and a RIGHT one both read as confident
science. Like is_meta, these are KEPT and keep maturing — they are just tagged
is_empirical_fact=1 and excluded from the science leaderboard's headline tiers,
so "what has the system DISCOVERED" is never answered by a lookup.

Deliberately high-precision, low-recall (mirrors meta_claim_classifier): only a
named entity that is the SUBJECT of an observation/announcement verb, or an
explicit "Did <entity> detect/observe/win/approve/launch ..." lookup question,
is flagged. A METHOD question that merely mentions an empirical domain stays
is_empirical_fact=0:
  FLAG:   "JWST discovered that TOI-561 b has no atmosphere"
  KEEP:   "Does spectral slope predict optimal denoising for real spectroscopic
           data (SDSS, JWST)?"   (JWST is an incidental data source, not the
           subject of a detection verb — this is a genuine method question)

Usage:
    from empirical_fact_classifier import is_empirical_fact
    python3 empirical_fact_classifier.py --report
    python3 empirical_fact_classifier.py --backfill        # is_empirical_fact IS NULL
    python3 empirical_fact_classifier.py --backfill --redo # reclassify everything
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discovery_routing as routing

# Observation / announcement verbs that make a named entity a LOOKUP subject.
_OBS_VERB = (r"(?:detect(?:ed|s)?|observ(?:ed|es)?|discover(?:ed|s)?|"
             r"imaged|measured|recorded|launch(?:ed|es)?|approv(?:ed|es)?|"
             r"awarded|won|announc(?:ed|es)?|reported\s+(?:the\s+)?detection)")

# Named instruments / agencies / missions that observe or announce.
_ENTITY = (r"(?:JWST|LIGO|Virgo|KAGR[A]?|Kepler|TESS|Hubble|HST|SPHEREx|"
           r"Gaia|NASA|ESA|the\s+EMA|the\s+FDA|Nobel(?:\s+Prize)?|"
           r"Event\s+Horizon\s+Telescope|Chandra|Fermi|Webb)")

EMPIRICAL_PATTERNS = [
    # "Did/Does <entity> <obs-verb> ..." — an explicit yes/no event lookup.
    (re.compile(r"\bDid\s+(?:a\s+[\w -]+?\s+)?" + _ENTITY + r"\b[\w ,'-]{0,60}?\b" + _OBS_VERB, re.I),
     "did-entity-observe lookup"),
    # "<entity> <obs-verb> ..." — declarative event assertion (entity as subject).
    (re.compile(r"^\s*(?:CONFIRMED:?\s*)?" + _ENTITY + r"\b[\w ,'-]{0,50}?\b" + _OBS_VERB + r"\b", re.I),
     "named-entity event assertion"),
    # Gravitational-wave catalog event as subject: "GW250114, detected/was/produced".
    (re.compile(r"\bGW\d{6}\b[\w ,'()-]{0,40}?(?:detect|was|is|produced|had|merger|SNR)", re.I),
     "named GW event lookup"),
    # Agency drug-approval tallies: "(FDA|EMA) approved N ... drugs ... in YYYY".
    (re.compile(r"\b(?:FDA|EMA)\b[\w ,'-]{0,30}?\bapprov\w+\b[\w ,'-]{0,30}?\bdrugs?\b", re.I),
     "agency approval tally"),
    # Award-recipient lookup: "the YYYY Nobel Prize ... to/went to/awarded/win".
    (re.compile(r"\bNobel\s+Prize\b[\w ,'-]{0,60}?\b(?:to|went\s+to|awarded|won|win)\b", re.I),
     "award-recipient lookup"),
    # Named-mission launch with date: "NASA launched X in <month> YYYY".
    (re.compile(r"\blaunch(?:ed)?\b[\w ,'-]{0,40}?\b(?:in\s+)?(?:January|February|March|April|May|June|"
                r"July|August|September|October|November|December|\d{4})\b", re.I),
     "dated mission launch"),
]


def is_empirical_fact(text):
    """Return (is_fact: bool, reason: str|None) for a claim hypothesis text.

    Two families of lookup are flagged. First, the named-instrument event
    assertions above (JWST/LIGO/NASA/Nobel/FDA as the subject of an observation
    verb) — high-precision, low-recall by design. Second, the lookup *shape* that
    the old whitelist missed and that leaked onto the shelf: recall or verification
    of an established published value — physical/astronomical constants, CODATA/NIST
    reference values, catalog figures. That detector is shared with the discovery
    router (discovery_routing._LOOKUP) so the classifier and the shelf agree on what
    a lookup is; #45167 ("recalls all 20 physical constants correctly, CODATA
    values") is exactly the case this second family adds."""
    if not text:
        return False, None
    for pat, reason in EMPIRICAL_PATTERNS:
        if pat.search(text):
            return True, reason
    lk = routing.is_lookup_fact(text)
    if lk:
        return True, f"established-value lookup ({lk!r})"
    return False, None


def ensure_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(knowledge_claims)")]
    if "is_empirical_fact" not in cols:
        conn.execute("ALTER TABLE knowledge_claims ADD COLUMN is_empirical_fact INTEGER")
        conn.commit()


def backfill(conn, redo=False, batch=2000):
    ensure_column(conn)
    where = "" if redo else "WHERE is_empirical_fact IS NULL"
    rows = conn.execute(
        f"SELECT id, hypothesis_text FROM knowledge_claims {where}").fetchall()
    n_fact = n_disc = 0
    pending = 0
    for row in rows:
        fact, _ = is_empirical_fact(row["hypothesis_text"])
        conn.execute("UPDATE knowledge_claims SET is_empirical_fact = ? WHERE id = ?",
                     (1 if fact else 0, row["id"]))
        n_fact += fact
        n_disc += (not fact)
        pending += 1
        if pending >= batch:
            conn.commit()
            pending = 0
    conn.commit()
    print(f"Backfilled {len(rows)} claims: {n_fact} empirical-fact, {n_disc} discovery")
    return n_fact, n_disc


def report(conn):
    ensure_column(conn)
    rows = conn.execute(
        "SELECT id, hypothesis_text, claim_status FROM knowledge_claims").fetchall()
    facts = []
    reasons = {}
    tiers = {}
    for row in rows:
        fact, reason = is_empirical_fact(row["hypothesis_text"])
        if fact:
            facts.append((row["id"], reason, row["hypothesis_text"]))
            reasons[reason] = reasons.get(reason, 0) + 1
            tiers[row["claim_status"]] = tiers.get(row["claim_status"], 0) + 1
    print(f"{len(facts)} / {len(rows)} claims classify as empirical-fact "
          f"({100.0 * len(facts) / max(len(rows), 1):.1f}%)")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {count:>6}  {reason}")
    print("by tier:", dict(sorted(tiers.items(), key=lambda x: -x[1])))
    print("\nSamples:")
    for cid, reason, text in facts[:12]:
        print(f"  #{cid} [{reason}] {(text or '')[:90]}")


if __name__ == "__main__":
    from db_retry import get_db
    ap = argparse.ArgumentParser(description="Tag lookup-fact claims (is_empirical_fact)")
    ap.add_argument("--report", action="store_true", help="Counts + samples, no writes")
    ap.add_argument("--backfill", action="store_true", help="Classify is_empirical_fact IS NULL")
    ap.add_argument("--redo", action="store_true", help="With --backfill: reclassify everything")
    args = ap.parse_args()
    conn = get_db()
    if args.report:
        report(conn)
    elif args.backfill:
        backfill(conn, redo=args.redo)
    else:
        ap.print_help()
    conn.close()
