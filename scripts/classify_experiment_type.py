"""
classify_experiment_type.py — heuristic fallback for experiment_type.

Workers SHOULD pass --type (MECHANISTIC | EMPIRICAL | EMPIRICAL_VALIDATED |
ANALOGICAL) via write_worker_result.py, but genuine-lane workers frequently
omit it, leaving experiment_type NULL/blank in prometheus.db. That makes those
experiments invisible to any typed dashboard/report/breakdown (the type system
in architecture-map.md). This module derives a best-effort type from the
signals available at apply time (tags + hypothesis/finding text), mirroring the
existing domain auto-classifier fallback.

Canonical definitions (from write_worker_result.py):
  MECHANISTIC         — does a known mechanism suffice? synthetic data OK.
  EMPIRICAL           — does a prediction hold vs real-world data?
  EMPIRICAL_VALIDATED — confirmed against actual physical measurements.
  ANALOGICAL          — does a mechanism from domain A also operate in domain B?

Precedence: ANALOGICAL > EMPIRICAL_VALIDATED > EMPIRICAL > MECHANISTIC.
MECHANISTIC is the documented majority and the safe default.
"""
import re

VALID_TYPES = ("MECHANISTIC", "EMPIRICAL", "EMPIRICAL_VALIDATED", "ANALOGICAL")

# Cross-domain / transfer signals -> ANALOGICAL
_ANALOGICAL_RE = re.compile(
    r"\b(analogical|cross[-\s]?domain|transfer(?:red|s|ring)?\b|"
    r"mechanism from .* (?:in|to) (?:another|a different|the .* )?domain|"
    r"does .* also (?:operate|hold|apply) in|generaliz\w+ (?:to|across) (?:the )?domain)",
    re.IGNORECASE,
)

# Confirmed against real measurements -> EMPIRICAL_VALIDATED
_VALIDATED_RE = re.compile(
    r"\b(validated against (?:real|actual|physical|measured)|"
    r"confirmed against (?:real|actual|physical|measured|observ)|"
    r"matches? (?:the )?(?:real|measured|observed|empirical) (?:data|measurements?)|"
    r"real[-\s]world measurements?|against (?:actual|physical) measurements?|"
    r"experimentally validated|field data confirm)",
    re.IGNORECASE,
)

# Tested against real / observed data -> EMPIRICAL
_EMPIRICAL_RE = re.compile(
    r"\b(real[-\s]?world data|observed data|measured data|empirical data|"
    r"historical data|dataset (?:from|of) (?:real|actual)|"
    r"observational|in[-\s]situ|sensor (?:data|readings)|"
    r"benchmark dataset|public dataset|against real)",
    re.IGNORECASE,
)


def classify_experiment_type(text="", tags=None, title=""):
    """Return one of VALID_TYPES. Best-effort; defaults to MECHANISTIC.

    text  : finding / result text (and/or hypothesis)
    tags  : list[str] or comma string of tags on the experiment
    title : experiment title (often carries the [TRANSFER] marker)
    """
    blob = " ".join(str(x) for x in (title or "", text or "")).strip()

    tag_list = []
    if tags:
        if isinstance(tags, str):
            tag_list = [t.strip().upper() for t in re.split(r"[,;]", tags) if t.strip()]
        else:
            tag_list = [str(t).strip().upper() for t in tags]

    # 1) ANALOGICAL — explicit marker wins (matches the lane semantics).
    if "TRANSFER" in tag_list or "ANALOGICAL" in tag_list:
        return "ANALOGICAL"
    if "[TRANSFER" in (title or "").upper() or "[TRANSFER" in (text or "").upper():
        return "ANALOGICAL"
    if _ANALOGICAL_RE.search(blob):
        return "ANALOGICAL"

    # 2) EMPIRICAL_VALIDATED — confirmed against real measurements.
    if _VALIDATED_RE.search(blob):
        return "EMPIRICAL_VALIDATED"

    # 3) EMPIRICAL — tested against real/observed data.
    if _EMPIRICAL_RE.search(blob):
        return "EMPIRICAL"

    # 4) Default — mechanism-sufficiency test (synthetic data OK). Majority case.
    return "MECHANISTIC"


if __name__ == "__main__":
    # Tiny self-test
    cases = [
        ("[TRANSFER] Does the Langmuir isotherm operate in soil chemistry?", [], "ANALOGICAL"),
        ("Confirmed against real measured coral SST data", [], "EMPIRICAL_VALIDATED"),
        ("Prediction tested against real-world sensor data", [], "EMPIRICAL"),
        ("Does Fick's second law produce hyperbolic diffusion profiles (synthetic)?", [], "MECHANISTIC"),
        ("cross-domain transfer of weight init to protein folding", ["TRANSFER"], "ANALOGICAL"),
    ]
    ok = 0
    for txt, tg, want in cases:
        got = classify_experiment_type(text=txt, tags=tg)
        flag = "ok" if got == want else "FAIL"
        ok += got == want
        print(f"  [{flag}] want={want:20s} got={got:20s}  {txt[:50]}")
    print(f"{ok}/{len(cases)} passed")
