#!/usr/bin/env python3
"""discovery_routing.py — decide what KIND of claim each candidate is.

The discovery pipeline conflated three orthogonal properties into one additive
score: ROBUSTNESS (survives replication + attack), NOVELTY (new to the world),
and NON-TRIVIALITY (a contingent fact, not a theorem / definition / lookup).
Robustness was measured well and owned ~60 of the 100 points; novelty was 40
points sourced from a single self-graded web search; non-triviality was not
represented at all. So a claim reached the top of the shelf by being robust,
regardless of whether it was new, derivable, or even a fact about the world.

This module supplies the two missing axes as pure, offline-testable text
classifiers plus a router, so a claim reaches the "discovery" shelf only when it
is robust AND grounded-novel AND contingent AND about-the-world. Everything here
is deterministic and network-free: it reads the text a claim's own subsystems
already produced (summary, novelty residue, audit citations + explanation,
adversarial-replication prose, mapped scope, prior-work citation) and returns a
route + a human-readable reason. No DB, no LLM.

Wire it into discovery_spotlight (persist the route) and discovery_report (rank
only the `discovery` route; render the other bins as the filter's honest output).

The routing is a first-match cascade, ordered so the safe error (declining to
call something novel) wins ties:

  1. known_in_lit   a publication id (PMID/DOI/arXiv/ISBN) or a set prior-work
                    citation appears in the claim's own evidence — it cannot also
                    be a first-documentation discovery
  2. empirical_fact is_empirical_fact, or lookup-shape prose (CODATA/NIST/
                    constants recall, catalog/reference values, official tallies)
  3. derivable      the claim's own prose says it is an identity / tautology /
                    analytically proven / a geometric property of the
                    representation — a theorem that survives attack is still a
                    theorem
  4. search_miss    NOT_FOUND at a confidence below the adequacy floor (kept: a
                    low-confidence "not found" is more likely a search miss)
  5. discovery      everything else; may carry a `sim_internal` FLAG (numbers are
                    parameter-dependent) that caps its novelty without hiding it
"""
import re

# ---- routes ---------------------------------------------------------------
DISCOVERY = "discovery"
KNOWN_IN_LIT = "known_in_lit"
EMPIRICAL_FACT = "empirical_fact"
DERIVABLE = "derivable"
SEARCH_MISS = "search_miss"

ROUTE_LABEL = {
    DISCOVERY: "candidate discovery",
    KNOWN_IN_LIT: "known — prior work identified",
    EMPIRICAL_FACT: "empirical-fact lookup",
    DERIVABLE: "derivable — analytic / definitional",
    SEARCH_MISS: "likely search miss",
}
# routes that are kept OFF the discovery shelf
OFF_SHELF = frozenset({KNOWN_IN_LIT, EMPIRICAL_FACT, DERIVABLE, SEARCH_MISS})

# ---- publication identifiers ---------------------------------------------
# a claim whose own evidence names one of these has a published referent
_PMID = re.compile(r"\bPMID\s*:?\s*(\d{6,9})\b", re.I)
_PMID_CTX = re.compile(r"\bPubMed\b[^.\n]{0,40}?\b(\d{7,9})\b", re.I)   # "PubMed (40555181)"
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+", re.I)
_ARXIV = re.compile(r"\barXiv\s*:?\s*(\d{4}\.\d{4,5})\b", re.I)
_ISBN = re.compile(r"\bISBN(?:-1[03])?\s*:?\s*[-0-9X ]{10,17}\b", re.I)

# literature-verification prose + named outlets: a real, reported result
_LIT_VERIFY = re.compile(
    r"\b(?:pubmed|e-?utilities|eutils|efetch|esearch|crossref|semantic scholar|"
    r"openalex|google scholar|peer[- ]reviewed|published in|the paper|preprint)\b", re.I)
_NEWS_OUTLET = re.compile(
    r"\b(?:CNN|Reuters|EurekAlert|ScienceDaily|BBC|Nature News|Cell\.com|phys\.org|"
    r"New York Times|NYT|Guardian|Associated Press|AP News)\b")

# ---- empirical-fact / lookup shapes --------------------------------------
# A lookup is recall/verification of established VALUES. Anchored on a verb or an
# adjacent value — NOT on bare "NIST"/"constants", which appear all over genuine
# method questions ("NIST 5-category defense structure", "does the transfer gap
# close when domains share the same physical constants"). Those must NOT be flagged.
_CONST_NOUN = re.compile(
    r"\b(?:physical|fundamental|astronomical|mathematical|universal)\s+constants?\b", re.I)
_RECALL_VERB = re.compile(
    r"\b(?:recall\w*|reproduc\w+|reprint\w*|verif\w+|retriev\w+|recit\w+|"
    r"maintain\s+accuracy|to\s+full\s+precision|correct\s+value)\b", re.I)
# CODATA/NIST adjacent to an actual value/constant (not NIST-as-taxonomy), plus
# the narrow official-tally / catalog shapes.
_VALUE_LOOKUP = re.compile(
    r"\b(?:CODATA(?:\s+\d{4})?\s+(?:recommended\s+)?(?:value|constant)|"
    r"NIST\s+(?:SP\s*\d|reference\s+value|recommended\s+value|CODATA)|"
    r"catalog(?:ue)?\s+value|reference\s+value\s+(?:for|of)|"
    r"official\s+(?:tally|count|figure)\s+(?:for|of|in))\b", re.I)

# A model correctly RECALLING/REPRODUCING established published values is a
# capability lookup, not a world discovery — even when the card frames itself as a
# "cross-family adversarial replication" (#45169: "what well-known mathematical
# facts do reasoning models handle correctly", "a model achieves 99.0% accuracy on
# 98 well-known mathematical facts"). This is the #45167 pattern in a new costume:
# #45167 carried a CODATA citation so known_in_lit caught it; #45169 has no citation
# and frames itself as a mechanism test, so only the recall SHAPE gives it away.
# Requires a known-ness qualifier ON a value-noun AND a model-recall verb within ~70
# chars, so a genuine method question that merely says "well-known" or "reproduce"
# in isolation does not trip.
_KNOWN_VALUE_NOUN = re.compile(
    r"\b(?:well[- ]known|textbook|canonical|famous|memoriz\w+|"
    r"stored\s+in\s+(?:the\s+)?(?:model\s+)?weights?)\s+"
    r"(?:\w+\s+){0,2}?(?:facts?|constants?|values?|identit\w+|quantit\w+|"
    r"results?|equations?|theorems?|numbers?|digits?|answers?)\b", re.I)
_MODEL_RECALL_VERB = re.compile(
    r"\b(?:recall\w*|reproduc\w+|verbatim|memoriz\w+|regurgitat\w+|"
    r"handles?\s+(?:them\s+|it\s+)?correctly|answers?\s+(?:them\s+)?correctly|"
    r"achiev\w+\s+[\d.]+\s*%\s*accuracy|"
    r"(?:reasoning\s+)?models?\s+(?:can\s+)?(?:\w+\s+){0,3}?"
    r"(?:recall|reproduc|handle|comput|answer|retriev))\b", re.I)

# ---- derivability / analytic triviality ----------------------------------
# STRONG: the claim's prose asserts it is a theorem/identity/definition
_DERIV_STRONG = re.compile(
    r"\b(?:mathematical(?:ly)?\s+identit\w*|is\s+an?\s+identity|tautolog\w*|"
    r"analytic(?:al)?ly\s+(?:proven|proved|prov\w+|deriv\w+|guaranteed|"
    r"necessary|necessit\w+)|mathematical\s+necessity|algebraic\s+identity|"
    r"definitional(?:ly)?|true\s+by\s+definition|closed[- ]form\s+identity)\b", re.I)
# MEDIUM: the claim is a property of the chosen representation, not the world
_DERIV_MED = re.compile(
    r"\b(?:geometric\s+propert\w+|propert\w+\s+of\s+(?:the\s+)?(?:tf-?idf\s+)?"
    r"(?:representation|vector\s+space|metric|embedding|encoding)|"
    r"baked\s+into\s+the\s+(?:representation|geometry|definition|metric)|"
    r"purely\s+(?:algebraic|geometric)|dimensional\s+analysis\s+alone)\b", re.I)

# ---- simulation-internal (a flag, not a route) ---------------------------
_SIM_RESIDUE = re.compile(
    r"\bthe\s+entire\s+(?:quantitative\s+claim|numeric\w*|simulation|"
    r"quantitative\s+result)\b", re.I)
_SIM_METHOD = re.compile(
    r"\b(?:monte[- ]carlo|simulated|simulation|toy\s+(?:model|setup)|"
    r"synthetic\s+(?:data|setup|benchmark)|self-?designed|"
    r"we\s+(?:designed|chose|set|swept)\s+(?:the\s+)?(?:parameter|threshold|regime))\b", re.I)

_NUM = re.compile(r"[-−]?\d+\.\d+")
# labeled quantities: "r=-0.819", "R^2 = 0.73", "p<0.01", "beta_c = 0.13", "F1=0.66"
# The leading lookbehind is load-bearing: without a word boundary the
# single-letter labels bleed out of word ENDINGS — "gap=25.7pp" and "gap>0.05"
# both read as p=…, which false-flagged the top two shelf cards (#60779
# compared its real p=0.049 against a 25.7pp gap; #66039 compared a measured
# gap=-0.025 against its own gap>0.05 bucketing threshold). The captured
# operator and unit let the reader separate reported statistics from
# thresholds and cross-unit lookalikes.
_LABELED = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<label>R\^?2|R²|r|rho|ρ|p|F1|AUC|beta_?c?|β_?c?|alpha|α|tau|τ|d|eta2?|η2?|"
    r"chi2|χ2|KS|MSE|RMSE|slope|threshold|t\*?|dt\*?)\s*(?P<op>[=:<>≈~≥≤]+)\s*"
    r"(?P<val>[-−]?\d+(?:\.\d+)?)"
    r"(?:\s*(?:to|-|–|—)\s*(?P<val2>[-−]?\d+(?:\.\d+)?))?"
    r"(?P<unit>\s?(?:pp|%))?",
    re.I)

# directional predicates: "underpredicts by 3.07x" / "overpredicts by 173%". A
# headline and residue asserting OPPOSITE polarities of the same card's central
# formula is the loudest self-disagreement a card can carry, and it never surfaces
# as a shared label=value pair (claim #68481: prose factors "3.07x"/"173%" carry no
# label). Negated mentions ("does NOT overpredict") are stripped before polarity is
# read, and a side that asserts BOTH polarities (regime prose) is ambiguous — no fire.
_DIR_NEG = re.compile(
    r"\b(?:do(?:es)?\s+not|did\s+not|doesn'?t|didn'?t|never|not)\s+"
    r"(?:actually\s+|significantly\s+|in\s+fact\s+)?(?:under|over)\s*-?\s*"
    r"(?:predict|estimat)\w*", re.I)
_DIR_PRED = re.compile(r"\b(under|over)\s*-?\s*(?:predict|estimat)\w*", re.I)

# A prediction the card QUOTES only to REJECT is not the card's own quantity, so
# its numbers must not enter the drift comparison. Claim #58935: "Side A's
# prediction that clipping destroys norm advantage (AUC~0.50) is REFUTED" — the
# card's real value is AUC 0.76, but the rejected 0.50 (first-mention-wins) was
# being compared against the residue's 0.74 and flagged as a self-contradiction.
# Blank these spans before reading labeled numbers / polarities, exactly as
# _DIR_NEG strips negations for the direction check. Bounded to the clause so a
# distant later REFUTED can't swallow the card's real values.
_REFUTED_PRED = re.compile(
    r"\bside\s+[ab]\b.{0,160}?\b(?:refuted|disproven|disproved|rejected)\b", re.I)


def _direction_polarity(text):
    """Set of un-negated 'under'/'over' (predict|estimate) polarities in `text`."""
    return {m.group(1).lower()
            for m in _DIR_PRED.finditer(_DIR_NEG.sub(" ", text or ""))}


def _blob(*parts):
    return " \n".join(p for p in parts if p)


def find_identifiers(text):
    """Return [(kind, matched_text), ...] for publication identifiers in `text`.
    A first-documentation discovery cannot cite the PMID/DOI of the very result
    it claims to be first to document."""
    text = text or ""
    out = []
    for kind, rx in (("PMID", _PMID), ("PMID", _PMID_CTX), ("DOI", _DOI),
                     ("arXiv", _ARXIV), ("ISBN", _ISBN)):
        for m in rx.finditer(text):
            out.append((kind, m.group(0).strip()))
    # dedupe preserving order
    seen, uniq = set(), []
    for k, v in out:
        key = (k, v.lower())
        if key not in seen:
            seen.add(key)
            uniq.append((k, v))
    return uniq


def has_named_publication(text):
    """True when the evidence both names a literature source AND a press outlet /
    'the paper' — the signature of a real, published, reported result even when no
    bare identifier was captured."""
    text = text or ""
    return bool(_LIT_VERIFY.search(text) and _NEWS_OUTLET.search(text))


def classify_derivability(text):
    """(strength, matched_terms). strength: 2 strong (identity/tautology/analytic),
    1 medium (property of the representation), 0 none. The signal already sits in
    the cards ('MATHEMATICAL IDENTITY', 'analytically proven', 'geometric property
    of TF-IDF vector space') — this reads it instead of ignoring it."""
    text = text or ""
    strong = _DERIV_STRONG.findall(text)
    if strong:
        return 2, _dedupe_lower(strong)
    med = _DERIV_MED.findall(text)
    if med:
        return 1, _dedupe_lower(med)
    return 0, []


def _dedupe_lower(items):
    seen, out = set(), []
    for it in items:
        s = (it if isinstance(it, str) else it[0]).strip()
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def is_lookup_fact(text):
    """The claim recalls/verifies established published values — physical constants,
    CODATA/NIST reference values, catalog figures, official tallies. Verb/value-
    anchored, and the recall verb must be NEAR the constants noun (≤60 chars) so a
    long multi-question claim that mentions "verification" in one sub-question and
    "fundamental constants" in another does not trip it. Returns the span, or None.

    Also catches the MODEL-RECALL costume: a claim whose finding is that a model
    correctly recalls/reproduces WELL-KNOWN facts/values/identities (#45169 — "what
    well-known mathematical facts do reasoning models handle correctly"). That is a
    capability lookup, not a discovery, however the card frames itself; it needs a
    known-ness qualifier ON a value-noun AND a model-recall verb within ~70 chars."""
    t = text or ""
    m = _VALUE_LOOKUP.search(t)
    if m:
        return m.group(0)
    verbs = [mo.start() for mo in _RECALL_VERB.finditer(t)]
    if verbs:
        for cn in _CONST_NOUN.finditer(t):
            if any(abs(v - cn.start()) <= 60 for v in verbs):
                return cn.group(0)
    # model correctly recalling/reproducing KNOWN published values (constants,
    # facts, identities) is a capability lookup regardless of card framing.
    known = [mo.start() for mo in _KNOWN_VALUE_NOUN.finditer(t)]
    if known:
        for mo in _MODEL_RECALL_VERB.finditer(t):
            if any(abs(mo.start() - k) <= 70 for k in known):
                return _KNOWN_VALUE_NOUN.search(t).group(0)
    return None


def simulation_flag(residue, summary="", scope=""):
    """A claim is 'model-internal' when its novel residue IS the entire output of a
    simulation the system designed itself — those numbers are properties of the
    chosen parameters, not facts the world surrendered. Returns a reason or None.
    This FLAGS + caps novelty; it does not hide the claim (robustness is real)."""
    res = residue or ""
    if _SIM_RESIDUE.search(res) and _SIM_METHOD.search(_blob(res, summary, scope)):
        return "novel residue is the entire output of a self-designed simulation"
    return None


def _labeled_quantities(text):
    """{normalized_label: (lo, hi)} for 'r=-0.819'-style statistics — a point is
    (v, v), a range ('p=2-3', 'R²=-3 to -97') is (min, max). Only LABELED numbers;
    a bare decimal (a sample size, a year, an axis bound) is ignored, so two
    unrelated numbers can't be mistaken for a disagreement. First mention wins."""
    out = {}
    for m in _LABELED.finditer(text or ""):
        if any(c in m.group("op") for c in "<>≥≤"):
            # A comparison operator binds a THRESHOLD/criterion ("gap>0.05",
            # "holds for d<2.5", "requires p<0.05"), not a reported statistic —
            # a point estimate must never be judged against a cutoff (#66039).
            continue
        v1, v2s = m.group("val"), m.group("val2")
        # A LONE integer is an iteration count / horizon / parameter setting / bound
        # ("T=20000", "p>0"→0), not a reportable statistic — only a decimal or an
        # EXPLICIT range (p=2-3) counts. Drops the false positives bare integers
        # introduce (#62239: t=20000 vs 8000) and lets the clean range win over the
        # "p>0/p=0" noise in #65186.
        if v2s is None and "." not in v1:
            continue
        lab = re.sub(r"[\s^_]", "", m.group("label")).lower()
        lab = {"r²": "r2", "χ2": "chi2", "η2": "eta2", "eta": "eta2",
               "βc": "betac", "β": "betac", "ρ": "rho", "α": "alpha",
               "τ": "tau", "t*": "t", "dt*": "dt"}.get(lab, lab)
        if m.group("unit"):
            # An explicit unit (pp/%) marks a different referent from a bare
            # value under the same letter — units never cross-compare.
            lab += "|" + m.group("unit").strip()
        lo = hi = float(v1.replace("−", "-"))
        if v2s:
            v2 = float(v2s.replace("−", "-"))
            lo, hi = min(lo, v2), max(lo, v2)
        out.setdefault(lab, (lo, hi))   # first mention wins (the headline value)
    return out


def _range_disagreement(a, b, rel_tol):
    """(repr_a, repr_b, drift) when intervals a=(lo,hi) and b=(lo,hi) do NOT overlap
    and the gap exceeds rel_tol of the magnitude; else None. A point is a zero-width
    interval, so two points reduce to the old |h-r|/max(|h|,|r|) drift exactly."""
    alo, ahi = a
    blo, bhi = b
    if max(alo, blo) <= min(ahi, bhi):       # intervals touch/overlap → agree
        return None
    if ahi < blo:                             # a entirely below b
        gap, ra, rb = blo - ahi, ahi, blo
    else:                                     # b entirely below a
        gap, ra, rb = alo - bhi, alo, bhi
    drift = gap / max(abs(alo), abs(ahi), abs(blo), abs(bhi), 1e-9)
    return (ra, rb, round(drift, 4)) if drift > rel_tol else None


def central_quantity_drift(headline, residue, scope="", rel_tol=0.05):
    """Flag ONLY when the SAME labeled quantity carries two different values across
    the headline finding and the novel residue — the card disagreeing with itself
    about the number it exists to report (r=-0.819 headline vs r=-0.707 residue).
    Returns (headline_val, residue_val, drift) for the worst-drifting shared label,
    or None. Requires a shared label, so unrelated decimals never trip it. Soft
    flag, not a removal.

    Also fires on a DIRECTION conflict — headline and residue each assert exactly
    one un-negated under/over-(predict|estimate) polarity and they disagree
    ("underpredicts by 3.07x" vs "overpredicts by 173%", #68481). Opposite signs
    expressed as prose factors never share a label=value pair, so the labeled pass
    alone is blind to the loudest possible mismatch. Returns
    (headline_dir, residue_dir, 1.0) in that case."""
    # Strip rejected-prediction spans ("Side A ... REFUTED") so a value the card
    # quotes only to disprove is not read as the card's own quantity (#58935).
    headline = _REFUTED_PRED.sub(" ", headline or "")
    residue = _REFUTED_PRED.sub(" ", residue or "")
    scope = _REFUTED_PRED.sub(" ", scope or "")
    # Compare each labeled quantity across finding and residue — the two texts
    # that restate the card's CENTRAL result — as intervals (overlap =
    # agreement), so a p that reads 2-3 in the finding but 0.25 in the residue
    # is caught (#65186); ranges were previously dropped entirely. The mapped
    # SCOPE is deliberately NOT in this pass: scope text reports statistics of
    # the narrowed surviving regime, which differ from the headline by
    # construction (#60779: headline classifier AUC=0.838 vs the boundary
    # map's own AUC=0.6125 — same label, different model), and its threshold
    # prose is criteria, not measurements (#66039). Scope still gets the
    # refuted-span stripping above and remains available to callers.
    srcs = [_labeled_quantities(headline), _labeled_quantities(residue)]
    worst = None
    for lab in set().union(*srcs):
        vals = [q[lab] for q in srcs if lab in q]
        for i in range(len(vals)):
            for j in range(i + 1, len(vals)):
                d = _range_disagreement(vals[i], vals[j], rel_tol)
                if d and (worst is None or d[2] > worst[2]):
                    worst = d
    if worst is None:
        hd, rd = _direction_polarity(headline), _direction_polarity(residue)
        if len(hd) == 1 and len(rd) == 1 and hd != rd:
            worst = (f"{next(iter(hd))}predicts", f"{next(iter(rd))}predicts", 1.0)
    return worst


def route_claim(*, summary="", hypothesis="", residue="", explanation="",
                citations="", scope="", adversarial_texts=(),
                prior_work_citation="", is_empirical_fact=False,
                novelty_confidence=None, conf_floor=0.55):
    """First-match cascade → (route, reason). See module docstring for the order.
    `adversarial_texts` is the list of this claim's adversarial-replication /
    hardening result prose — where a worker's own 'PMID 40555181' confession lives."""
    lit_blob = _blob(summary, hypothesis, residue, explanation, citations, scope,
                     prior_work_citation, *adversarial_texts)

    # 1. known in the literature — a real published referent exists
    ids = find_identifiers(lit_blob)
    if ids:
        return KNOWN_IN_LIT, f"publication id in evidence: {ids[0][0]} {ids[0][1]}"
    if (prior_work_citation or "").strip():
        return KNOWN_IN_LIT, f"prior-work citation set: {prior_work_citation.strip()[:90]}"
    if has_named_publication(lit_blob):
        return KNOWN_IN_LIT, "literature-verification + named-outlet prose in evidence"

    # 2. empirical-fact lookup
    if is_empirical_fact:
        return EMPIRICAL_FACT, "is_empirical_fact=1"
    lk = is_lookup_fact(_blob(summary, hypothesis, residue))
    if lk:
        return EMPIRICAL_FACT, f"lookup-shape prose: {lk!r}"

    # 3. derivable / analytic triviality
    strength, terms = classify_derivability(_blob(summary, hypothesis, residue, scope))
    if strength >= 1:
        kind = "strong" if strength >= 2 else "representation"
        return DERIVABLE, f"derivability ({kind}): {terms[0]!r}"

    # 4. low-confidence NOT_FOUND — probably a search miss, not novelty
    if novelty_confidence is not None and float(novelty_confidence) < conf_floor:
        return SEARCH_MISS, f"novelty confidence {float(novelty_confidence):.2f} < {conf_floor:.2f}"

    # 5. genuine candidate (possibly flagged model-internal)
    return DISCOVERY, "robust; no prior-work id, lookup shape, or derivability signal"


# --------------------------------------------------------------------------
# self-test — runs the four claims Opus flagged through the router. `python3
# discovery_routing.py` must print ALL PASS or exit non-zero.
# --------------------------------------------------------------------------
def _selftest():
    cases = [
        # 62245 allokelping — PMID sits in the hardening prose; audit said NOT_FOUND@0.95
        dict(name="allokelping (#62245)", expect=KNOWN_IN_LIT,
             kw=dict(summary="DISCOVERY HARDENING independent re-derivation of claim #62245 "
                             "(orca kelp allogrooming tool manufacture).",
                     residue="The entire observation of allokelping as a tool manufacture and "
                             "allogrooming behavior including the claim of first documentation.",
                     novelty_confidence=0.95,
                     adversarial_texts=[
                         "SUPPORTED: ATTACK_OUTCOME: SURVIVED. Independent PubMed verification via "
                         "E-utilities API confirmed PMID 40555181: 'Manufacture and use of "
                         "allogrooming tools by wild killer whales'.",
                         "Paper independently verified via PubMed (40555181), Cell.com, EurekAlert, "
                         "CNN, Reuters, Guardian."])),
        # 45167 CODATA constants — prior_work_citation is set AND lookup-shape prose
        dict(name="CODATA constants (#45167)", expect=KNOWN_IN_LIT,
             kw=dict(summary="Independent cross-family re-derivation across 20 physical/astronomical "
                             "constants. Model recalls ALL 20 constants correctly with SI units.",
                     prior_work_citation="CODATA 2022, NIST SP 961 (May 2024) — physics.nist.gov/constants",
                     novelty_confidence=0.95)),
        # CODATA again, but pretend the citation wasn't set — lookup shape must still catch it
        dict(name="CODATA w/o citation", expect=EMPIRICAL_FACT,
             kw=dict(summary="Model recalls all 20 physical constants correctly (CODATA values).",
                     novelty_confidence=0.95)),
        # 65180 TF-IDF centroid ~ F1 — a geometric property of the representation
        dict(name="TF-IDF geometric (#65180)", expect=DERIVABLE,
             kw=dict(summary="Negative correlation between inter-class TF-IDF centroid similarity and "
                             "F1. The correlation is a GEOMETRIC property of TF-IDF vector space.",
                     residue="The specific quantitative correlation (mean r=-0.707).",
                     novelty_confidence=0.85)),
        # beta_c epidemic threshold — a stated mathematical identity
        dict(name="beta_c identity", expect=DERIVABLE,
             kw=dict(summary="CV_total vs beta_c correlation. beta_c = <k>/(<k^2>-<k>); at fixed mean "
                             "degree beta_c ~ 1/CV^2 — this is a MATHEMATICAL IDENTITY, not an "
                             "empirical correlation.",
                     novelty_confidence=0.60)),
        # dt*/dp Bayesian threshold — analytically proven
        dict(name="dt*/dp analytic", expect=DERIVABLE,
             kw=dict(summary="The optimal shape-similarity threshold is a MATHEMATICAL NECESSITY of "
                             "the Bayesian decision framework. Analytically proven: dt*/dp < 0 for "
                             "all p in (0,1).",
                     novelty_confidence=0.90)),
        # a genuine-looking contingent finding — must stay on the shelf
        dict(name="genuine candidate", expect=DISCOVERY,
             kw=dict(summary="Buried-residue tolerance slope flips sign at a coupling the theory did "
                             "not predict, confirmed across 7 semiconductors.",
                     residue="The sign-flip location and its magnitude.",
                     novelty_confidence=0.80)),
        # low-confidence not-found — search miss, not novelty
        dict(name="low-conf not found", expect=SEARCH_MISS,
             kw=dict(summary="Some vague relationship between two quantities.",
                     novelty_confidence=0.30)),
        # 45169 — a model correctly recalling well-known math FACTS is a capability
        # lookup, not a discovery (the #45167 constants-recall pattern in a new
        # costume: no citation, framed as "cross-family adversarial replication").
        dict(name="model-recall of facts (#45169)", expect=EMPIRICAL_FACT,
             kw=dict(summary="ATTACK_OUTCOME: SURVIVED. Cross-family adversarial replication of claim "
                             "#45169: deepseek-v4-flash independently tested 26 mathematical facts. "
                             "Model got 25/26 (96.2%) correct.",
                     hypothesis="What other well-known mathematical facts do reasoning models handle correctly?",
                     residue="The entire claim: a model achieves 99.0% accuracy on 98 well-known "
                             "mathematical facts, with the only failure being ln(2) beyond 15 decimals.",
                     novelty_confidence=0.90)),
    ]
    ok = True
    for c in cases:
        route, reason = route_claim(**c["kw"])
        good = route == c["expect"]
        ok = ok and good
        print(f"  [{'PASS' if good else 'FAIL'}] {c['name']:26s} -> {route:14s} ({reason})")
        if not good:
            print(f"         expected {c['expect']}")
    # drift detector
    drift = central_quantity_drift("Mean r=-0.819 across 8 valid tests (range [-0.866, -0.652])",
                                   "The specific quantitative correlation (mean r=-0.707, range "
                                   "[-0.749, -0.489])")
    dgood = drift is not None and abs(drift[2] - 0.137) < 0.02
    ok = ok and dgood
    print(f"  [{'PASS' if dgood else 'FAIL'}] r-drift detector          -> {drift}")
    # direction-conflict drift — #68481: headline "underpredicts by 3.07x … does
    # NOT overpredict as Side B claimed" vs residue "overpredicts by 173%"
    dconf = central_quantity_drift(
        "The formula ITSELF underpredicts empirical CV² by 3.07x on BA and 3.41x on "
        "Config — it does NOT overpredict as Side B claimed. At γ=3.0 the "
        "underprediction worsens to 6.23x.",
        "The finding that the corrected CV formula overpredicts by 173%, 118%, and "
        "89% for specific scale-free models appears to be absent from published work.")
    cgood = dconf == ("underpredicts", "overpredicts", 1.0)
    ok = ok and cgood
    print(f"  [{'PASS' if cgood else 'FAIL'}] direction-conflict (#68481)-> {dconf}")
    # a side asserting BOTH polarities is ambiguous regime prose — must NOT fire
    damb = central_quantity_drift(
        "The model underpredicts at low k but overpredicts at high k.",
        "The formula overpredicts by 40% in the high-k regime.")
    agood = damb is None
    ok = ok and agood
    print(f"  [{'PASS' if agood else 'FAIL'}] ambiguous polarity no-fire -> {damb}")
    # 67218 — labeled R² drift: headline "LR R²=1.00" (smooth regime) vs residue
    # "R²<0.5" (the 41.7%-failure regime). The card reports two very different values
    # for the quantity it exists to report; the flag keeps that visible. Frozen for
    # regression alongside #68481 (the reviewer asked for both to stay covered).
    d67 = central_quantity_drift(
        "ARBITRATION_VERDICT: REGIME_SPLIT. Smooth classifiers (LR R²=1.00) have high "
        "R². Non-smooth classifiers (RF R²=-3 to -97) fail.",
        "The entire quantitative claim: 41.7% failure rate (10/24 experiments, R²<0.5), "
        "label noise >20% destroys the quadratic form.")
    d67good = d67 == (1.0, 0.5, 0.5)
    ok = ok and d67good
    print(f"  [{'PASS' if d67good else 'FAIL'}] labeled-drift (#67218)     -> {d67}")
    # 58935 — refuted-prediction false-positive guard: the card QUOTES Side A's
    # rejected AUC~0.50 only to disprove it (the card's real value is 0.76). Before
    # the strip, finding auc=0.50 (first-mention) was compared against the residue's
    # 0.74 and flagged as a self-contradiction. Must NOT fire now.
    d589 = central_quantity_drift(
        "Side A's prediction that clipping destroys norm advantage (AUC~0.50) is REFUTED.",
        "norm AUC ~0.74-0.77 vs sparsity AUC ~0.45-0.47.")
    d589good = d589 is None
    ok = ok and d589good
    print(f"  [{'PASS' if d589good else 'FAIL'}] refuted-pred no-fire (#58935)-> {d589}")
    # 65186 — range + scope: "best p=2-3" (finding) vs "p=0.25" (residue), with the
    # lone "p>0/p=0" conditions skipped so the clean range wins. Was a false negative
    # (finding extracted nothing) before ranges/integers + a scope comparison; the
    # overlapping p=1-2 in scope must NOT be what fires. Must FIRE on (2.0, 0.25).
    d651 = central_quantity_drift(
        "Formulation A: p>0 beats p=0, best p=2-3. Formulation B: p=0 optimal.",
        "The optimal p=0.25 and the exact comparisons.",
        scope="regime with best p=1-2")
    d651good = d651 is not None and d651[0] == 2.0 and d651[1] == 0.25
    ok = ok and d651good
    print(f"  [{'PASS' if d651good else 'FAIL'}] range+scope drift (#65186) -> {d651}")
    # simulation flag
    sim = simulation_flag("the entire quantitative claim", "Monte Carlo simulation we designed")
    sgood = sim is not None
    ok = ok and sgood
    print(f"  [{'PASS' if sgood else 'FAIL'}] sim-internal flag          -> {sim}")
    print("\n" + ("ALL PASS" if ok else "*** FAILURES ***"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
