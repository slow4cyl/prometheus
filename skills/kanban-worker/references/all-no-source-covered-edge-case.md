# All NO_SOURCE Items Covered — Edge Case (Cycle #267)

## Scenario

When running the Director pass, all NO_SOURCE queue items have MED+ keyword overlap with running experiment titles. Even though we're below saturation (<7 running), creating new tasks would waste workers on work that running experiments will resolve.

## Detection

Run the quantitative coverage scoring (Cycle #177) on all NO_SOURCE items:

```python
stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'from'}
keywords = {re.sub(r'^exp_\d+\w*$', '', w) for w in words - stop if not re.match(r'^exp_\d+\w*$', w) and len(w) > 2}
running_titles_text = ' '.join(t.get('title', '').lower() for t in running)
overlap = sum(1 for w in keywords if w in running_titles_text)
coverage = overlap / max(len(keywords), 1)
# HIGH >0.3 (likely covered), MED 0.15-0.3 (partially covered), LOW <0.15 (open)
```

## Decision

If ALL NO_SOURCE items have MED+ coverage:
- **Do NOT create new experiment tasks** — running experiments will resolve them
- **Do create synthesis tasks** if unsynthesized experiments exist (maintenance, not investigation)
- **Do create curation tasks** if queue needs tagging/cleanup (maintenance, not investigation)
- **Log the pass** and chain to next cycle

## Real Example (Cycle #267)

Board: 7 running tasks, 9 workers busy, 13 free.
Queue: 11 NO_SOURCE items, all with MED+ coverage (0.15-0.44).
Action: Created synthesis task (3 unsynthesized experiments) + curation task (tag resolved items). Did NOT create new experiment tasks — all queue items covered.

## Why This Matters

Without this check, the Director would create tasks for items like "INT1 binary edge classifier" even though exp_1435 (INT1 binary + quantized embeddings) is already running. The running experiment will answer the queue item's question when it completes and synthesis processes it. Creating a duplicate wastes a worker slot.
