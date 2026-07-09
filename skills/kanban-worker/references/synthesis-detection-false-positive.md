# Synthesis Task Detection — False Positive from Queue Source Text

**Added:** Cycle 270 (June 2026)

## Problem

When filtering running tasks to identify synthesis tasks, naive keyword matching produces false positives. Experiment titles originating from curiosity queue items contain source attribution like `[NEW from synthesis v651]` — the word "synthesis" in the source text triggers the filter, incorrectly classifying regular experiments as synthesis tasks.

## Example

Cycle 270 — 22 running tasks, 1 actual synthesis task:
- `t_2b063b30: SYNTHESIS: 29 unsynthesized experiments (exp_182b-exp_2641)` ← real synthesis
- `t_425c6e3d: exp_2623: [NEW from synthesis v535] Standalone TF-IDF+LR...` ← false positive
- `t_ae34bdd2: exp_2651: [NEW from synthesis v640] Neural embeddings...` ← false positive

Filtering by `'synth' in title.lower()` returned 12 matches out of 22 — only 1 was real.

## Fix

```python
# BROKEN — matches "synthesis vNNN" in queue source text
synth_tasks = [t for t in running if 'synth' in t.get('title','').lower()]

# CORRECT — matches only actual synthesis task titles
synth_tasks = [t for t in running 
               if t.get('title','').startswith('SYNTHESIS') 
               or 'consolidat' in t.get('title','').lower()]
```

The key insight: real synthesis tasks use ALL-CAPS "SYNTHESIS:" as a title prefix. Queue-sourced experiments have lowercase "synthesis vNNN" inside bracket attribution. Case-sensitive prefix matching avoids the collision.

## Broader Pattern

This is a special case of the "keyword in metadata vs keyword in content" collision. When filtering by semantic role (synthesis vs experiment), use structural signals (title prefix, task body keywords) rather than free-text keyword matching.
