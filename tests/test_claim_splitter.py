"""Pure-function invariants for the spurious-support splitter."""
import claim_splitter as cs


def _sub(**over):
    d = dict(hypothesis="Does averaging predictions from independently noisy models improve ECE?",
             iv="ensemble size", dv="calibration error", dataset_or_process="synthetic logits",
             intervention="average K model predictions", metric="ECE", baseline="single model",
             expected_direction="decrease", regime="independent errors", grounded_in=["exp_1"])
    d.update(over)
    return d


def test_valid_subclaims_requires_schema_density():
    full = _sub()
    thin = {"hypothesis": "Does it work?", "iv": "x"}          # < 6 schema fields
    empty = {"iv": "x", "dv": "y"}                             # no hypothesis
    out = cs.valid_subclaims({"subclaims": [full, thin, empty]})
    assert out == [full]


def test_valid_subclaims_caps_at_max():
    subs = [_sub(hypothesis=f"Q{i}?") for i in range(8)]
    assert len(cs.valid_subclaims({"subclaims": subs})) == cs.MAX_SUBCLAIMS


def test_curiosity_text_embeds_the_preregistration_schema():
    t = cs.curiosity_text(_sub())
    assert t.startswith("Does averaging predictions")
    assert "[SPLIT-SCHEMA" in t
    for marker in ("IV=", "DV=", "metric=", "baseline=", "direction=", "regime="):
        assert marker in t


def test_parse_json_block_tolerates_reasoning_prose():
    wrapped = 'Thinking about it...\n{"splittable": true, "reason": "ok", "subclaims": []}\nDone.'
    got = cs.parse_json_block(wrapped)
    assert got == {"splittable": True, "reason": "ok", "subclaims": []}
    assert cs.parse_json_block("no json here") is None
    assert cs.parse_json_block("") is None


def test_sa_threshold_mirrors_routing_and_maturity():
    import discovery_routing as routing
    assert cs.SA_MAX == routing.SPURIOUS_SA_MAX == 0.6


# --- mechanism_evidence_critic pure functions --------------------------------

def test_mechanism_critic_parse_and_verdict_logic():
    import mechanism_evidence_critic as mec
    got = mec.parse_review('blah {"verdict": "CONTRADICTED", "confidence": 0.9, "reason": "r=-0.78 not +"}')
    assert got["verdict"] == "CONTRADICTED" and got["confidence"] == 0.9
    assert mec.parse_review("no json") is None
    # marker filter: claims without a mechanism story are never judged
    assert mec._MECH_MARKERS.search("WHY IT WORKS: adaptive normalization")
    assert mec._MECH_MARKERS.search("the effect is driven by tail mass")
    assert not mec._MECH_MARKERS.search("r=0.5 across 7 domains, p<0.01")
