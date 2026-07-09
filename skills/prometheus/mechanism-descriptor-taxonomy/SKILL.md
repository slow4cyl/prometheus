---
name: mechanism-descriptor-taxonomy
description: 5-state transfer classification for Prometheus lineage abstraction dynamics. PROPERTY is a hub not a stepping stone. Ambiguity collapses from 30 to 4.5 percent. Winning lineages switch states rather than locking into any one.
version: 1.0.0
---

# Mechanism-Descriptor Taxonomy for Prometheus Lineages

## The 5 States

| State | Definition | Examples |
|-------|-----------|---------|
| MECHANISM | Causal process | ionophore binding, attention dilution, feedback cascade |
| PROPERTY | Named behavior | dominance pattern, timing effect, selectivity advantage |
| DESCRIPTOR | Mathematical pattern | power law, Pareto optimality, universality class |
| DOMAIN_PAIR | Domain transfer without named concept | Does biology transfer to drug resistance? |
| AMBIGUOUS | Truly unresolvable | Malformed questions, broken references |

## Key Findings (June 21, 2026)

### Distribution (depth >= 5, 5-state with morphology normalization)
DESCRIPTOR 42%, PROPERTY 35%, MECHANISM 21%, DOMAIN_PAIR 1%, AMBIGUOUS 1%

Morphology normalization drops AMBIGUOUS from 4.5% to ~1% by mapping verb/adjective forms to canonical noun forms (modulate→modulation, coupled→coupling, catalytic→catalysis, correlate→correlation, etc.). The 1% irreducible floor is domain-specific nouns and methodological questions beyond keyword classification.

### Ambiguity Resolution
The 4-state classifier had 29.9% AMBIGUOUS. Adding PROPERTY collapses ambiguity to 4.5%.

### Transition Matrix
PROPERTY is the hub — the only state connected to both MECHANISM and DESCRIPTOR:
- MECHANISM to PROPERTY: 26%
- PROPERTY to DESCRIPTOR: 18%
- DESCRIPTOR to PROPERTY: 25% (bidirectional P-D loop)
- AMBIGUOUS to PROPERTY: 35% (resolution path)

### First Appearance Depth
PROPERTY earliest (2.7), then DESCRIPTOR (3.0), then MECHANISM (3.8). The system starts with "what happens?" before "why?"

### Causality
At shallow depths (5-15), early descriptor-displacement predicts WORSE outcomes. At depth 30+, descriptor-displaced lineages dominate. This is survivorship bias: good lineages survive long enough to reach descriptors.

### Winning Grammar
Deep lineages are enriched in state-switching bigrams:
- MECHANISM to DESCRIPTOR +2.7pp
- DESCRIPTOR to MECHANISM +2.2pp
- PROPERTY to PROPERTY +1.5pp

Deep lineages are depleted in lock-in:
- DESCRIPTOR to DESCRIPTOR -1.7pp
- DESCRIPTOR to AMBIGUOUS -2.0pp

## Scripts
- mechanism_descriptor_instrument.py — 4-state classifier, lineage tracing, fertility correlation
- deep_lineage_analysis.py — ambiguity resolution, transition matrix, predictiveness
- abstraction_ladder.py — 5-state classifier, staircase test, depth-by-state

## Monitoring
Cron job ddf39e040d0b (every 4h) for mechanism-descriptor-monitor. Snapshot at ~/.hermes/mechanism_descriptor_snapshot.json.