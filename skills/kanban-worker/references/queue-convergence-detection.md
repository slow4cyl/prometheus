# Queue Convergence Detection

Added: Cycle #245 (2026-06-02)

## Pattern

When the Director classifies all queue items and finds that:
- ALL NO_SOURCE items have HIGH coverage (≥0.3) against running task titles
- ALL DONE items are covered by running tasks (source experiment still running)
- No UNKNOWN items exist

...the queue is **CONVERGED** — every active research question is already being investigated by a running experiment.

## Signal Meaning

Queue convergence is the opposite of saturation:
- **Saturation**: too many tasks running, can't create more → synthesis only
- **Convergence**: all questions covered, nothing genuinely uncovered → synthesis + pivot to new research

Both warrant synthesis (maintenance), but convergence additionally signals the current research thread is mature and the Director should generate new research directions.

## Detection Code

```python
import json, re, os

running = json.load(open('/tmp/kanban_running.json'))
running_exp_ids = set()
running_titles = [t.get('title', '') for t in running]
for t in running:
    for m in re.finditer(r'exp_(\d+\w*)', t.get('title', '')):
        running_exp_ids.add(f'exp_{m.group(1)}')

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'does'}

uncovered_count = 0
for item in queue:
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text:
        continue
    source_ids = re.findall(r'exp_(\d+\w*)', text)
    if not source_ids:
        # NO_SOURCE — check coverage
        words = set(re.findall(r'\b\w+\b', text.lower()))
        keywords = {w for w in words - stop if not re.match(r'^exp_\d+$', w) and len(w) > 2}
        running_text = ' '.join(t.lower() for t in running_titles)
        overlap = sum(1 for w in keywords if w in running_text)
        coverage = overlap / max(len(keywords), 1)
        if coverage < 0.3:
            uncovered_count += 1
    elif not any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
        # DONE item — check if source is in self_state but question still open
        uncovered_count += 1

if uncovered_count == 0:
    print("QUEUE CONVERGED: all items covered by running tasks")
else:
    print(f"Queue has {uncovered_count} uncovered items")
```

## Director Action on Convergence

1. Create synthesis task for unsynthesized experiments (maintenance)
2. Log convergence as a signal in the audit trail
3. In QUEUE NEXT step, generate 3-5 new research directions based on:
   - Gaps identified during synthesis (what questions remain open?)
   - Novel angles not yet explored (different attack vectors, deployment scenarios)
   - Cross-domain transfer opportunities (results from one domain applied to another)
4. Do NOT create experiment tasks just to fill free worker slots — idle workers are fine when the queue is converged

## Real-World Example (Cycle #245)

Director pass found:
- 4 running tasks covering all active queue topics
- 14 NO_SOURCE items, all with HIGH coverage (0.39-0.55) against running titles
- 4 DONE items, all covered by running tasks
- Queue converged — no new experiment tasks created
- Synthesis task created for 10 unsynthesized experiments
- 18 free workers left idle (correct behavior — no uncovered items to investigate)

## Extended Failure Mode: Full System Stall (June 5 2026)

The original convergence detection checks queue items against RUNNING tasks. A worse variant exists: **all queue items match COMPLETED experiments** (not just running ones), AND adaptive cap=0 (synthesis blocked), AND impact ratio=0% (no BUILD implementations). This creates a complete system stall — no experiments can be created, no synthesis can run, no BUILD tasks exist.

**Detection signature:**
- `batch_create_tasks.py` returns "Uncovered by all dedup layers: 0" despite queue items
- `queue_curator.py --dry-run` reports 0 stale items (items are clean, just semantically covered)
- Adaptive cap = 0 (realtime ROI = 0.000)
- Impact ratio = 0% (0/N BUILD-tagged experiments implemented)
- All queue items word-overlap ≥0.5 with completed experiment hypotheses

**Root cause:** The synthesis → curiosity → experiment → synthesis loop converges to paraphrases of completed work. Jaccard + phrase dedup correctly blocks duplicates, but there's no mechanism to inject genuinely novel curiosity sources.

**Escape strategies:**
1. **External domain injection** — manually add 10-15 curiosity items from unexplored domains (code injection, multi-modal, real-world deployment)
2. **Cross-pollination from RAG** — query `experiment_rag.py query "transferable mechanism"` for findings suggesting transfer to unexplored areas
3. **BUILD as novelty source** — implement a confirmed finding (e.g., TF-IDF filter), test on real traffic, discover failure modes → new curiosities
4. **Queue reset** — clear queue, let synthesis rebuild from scratch with manually curated seed questions

**See also:** `director-loop` skill `references/steady-state-convergence-trap.md` for full diagnostic code and prevention strategies.

## Contrast with "Free Workers During Saturation"

The "free workers during saturation" rule (Cycle #166) says don't create tasks just because slots exist when ≥7 tasks are running. Queue convergence extends this: don't create tasks just because slots exist when ALL questions are covered, even if <7 tasks are running.

The key insight: worker utilization is not the goal — question coverage is. Idle workers are fine when there's nothing genuinely uncovered to investigate.
