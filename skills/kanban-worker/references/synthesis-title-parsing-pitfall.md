# Synthesis Title Comma-Separated Experiment ID Format

## Problem

Synthesis task titles use comma-separated numbers after the first `exp_` prefix:

```
"SYNTHESIS: consolidate exp_808, 809, 811, 817, 821, 822, 823, 834, 835"
```

A naive `re.findall(r'exp_(\d+)', title)` only matches `exp_808` — the remaining IDs (809, 811, etc.) lack the `exp_` prefix and are missed.

## Impact

- Director thinks only 1 experiment is covered by synthesis
- Wastes a `kanban_comment` adding experiments already in scope
- False "unsynthesized" count inflated

## Fix: Two-Step Parser

```python
import re

def extract_synthesis_ids(title):
    """Extract all experiment IDs from a synthesis task title."""
    synthesis_covered = set()
    first_match = re.search(r'exp_(\d+)', title)
    if first_match:
        synthesis_covered.add('exp_' + first_match.group(1))
        rest = title[first_match.end():]
        for m in re.finditer(r'(\d+)', rest):
            synthesis_covered.add('exp_' + m.group(1))
    return synthesis_covered

# Example:
title = "SYNTHESIS: consolidate exp_808, 809, 811, 817, 821, 822, 823, 834, 835"
ids = extract_synthesis_ids(title)
# Returns: {'exp_808', 'exp_809', 'exp_811', 'exp_817', 'exp_821', 'exp_822', 'exp_823', 'exp_834', 'exp_835'}
```

## When to Use

- When checking if a running synthesis task covers unsynthesized experiments
- When expanding synthesis scope via `kanban_comment`
- When comparing synthesis task coverage against `self_state.json`

## Observed In

- Cycle #219: Director detected 13 unsynthesized experiments, initially thought synthesis only covered 1 (exp_808). Two-step parser revealed it covered 9, leaving only 4 to add.
