# Director Pass Refinements — Cycle #254

## 1. Single-Experiment Synthesis Exception

**Current rule:** ≥3 unsynthesized → CREATE SYNTHESIS TASK. <3 → skip.

**Refinement:** When only 1-2 experiments are unsynthesized but they have high-impact findings, create a synthesis task immediately.

**Detection criteria (any one suffices):**
- Summary contains "REFUTED", "NOVEL", "BREAKTHROUGH"
- AUC improvement >5pp over baseline
- New mechanism or architectural insight discovered
- Hypothesis confirmed or denied with strong evidence

**Rationale:** Delaying knowledge integration for a count threshold wastes the synthesis worker's capacity on trivial consolidation. The synthesis worker handles single experiments as efficiently as batches.

**Example:** exp_1376 ("Self-training actively hurts performance -4% to -6% AUC") was the only unsynthesized experiment but had high-impact findings. Synthesis was created immediately rather than waiting for 2 more experiments.

## 2. Coverage Scoring as Automated "Still Open?" Check for DONE Items

**Current approach:** After classifying DONE items, manually check each source experiment's findings to determine if the queue item's question was answered.

**Refinement:** Use quantitative coverage scoring to automate the "still open?" check. Items whose source experiments are completed but whose topics aren't investigated by running experiments are candidates for new tasks.

**Method:**
```python
# After classifying DONE items, apply coverage scoring
for item in done_items:
    words = set(re.findall(r'\b\w+\b', item_text.lower())) - stop_words
    keywords = {w for w in words if len(w) > 2 and not re.match(r'^exp_\d+$', w)}
    running_text = ' '.join(t.lower() for t in running_titles)
    overlap = sum(1 for w in keywords if w in running_text)
    coverage = overlap / max(len(keywords), 1)
    
    if coverage < 0.15:  # LOW coverage = topic not being investigated
        # Candidate for new task — question is still open
        candidates.append(item)
    elif coverage > 0.3:  # HIGH coverage = likely covered by running experiment
        # Skip — running experiment will resolve this
        pass
    else:  # MED coverage = partially covered
        # Manual check needed — running experiment may or may not address this
        manual_check.append(item)
```

**Threshold rationale:**
- LOW (<0.15): Fewer than 15% of the item's keywords appear in running task titles → topic is genuinely uncovered
- MED (0.15-0.30): Partial overlap → running experiment may address this, but uncertain
- HIGH (>0.30): Significant overlap → running experiment likely covers this question

**When to use:** During Director passes with ≥7 running tasks where manual review of each DONE item is impractical. The coverage scoring filters candidates efficiently, leaving only LOW-coverage items for task creation.

## 3. Low-Coverage Items Still Deferred When Topically Related to Running Experiments

**Scenario:** Coverage scoring returns LOW (<0.15) for a queue item, suggesting it's uncovered. But the item is topically related to a running experiment even though the keyword overlap is low (different phrasing, different aspect of the same topic).

**Example:** Queue item "MAML accuracy-efficiency tradeoff" scores 0.07 coverage because the running task is titled "MAML meta-learning generalization" — different keywords, same research area. The running experiment may produce findings that inform the uncovered question.

**Decision rule:** When coverage is LOW but manual inspection reveals topical overlap with a running experiment, defer the item. The running experiment's synthesis may resolve it. Only create a new task if:
1. The uncovered question is genuinely independent (different hypothesis, different method), OR
2. The running experiment is unlikely to address this specific angle (e.g., different domain, different model), OR
3. The queue item has been deferred for 2+ cycles without resolution.

**Rationale:** Keyword-based coverage scoring misses semantic relationships. "MAML accuracy-efficiency" and "MAML generalization" share the topic (MAML) but few keywords. A human would recognize they're related; the scoring algorithm doesn't. The cost of deferring (1 cycle delay) is lower than the cost of creating a redundant task (wasted worker slot).
