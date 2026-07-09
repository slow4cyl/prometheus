# Director Queue Triage Patterns (Cycle #201, updated Cycle #245)

## Flowchart integration note

Step 4 of the Director Pass Flowchart says "DONE (check if question answered)" — this means run coverage scoring against ALL running task titles, not just check the source experiment. A DONE item whose source experiment is completed but whose question is still being investigated by a DIFFERENT running experiment should be classified as effectively RUNNING (skip). The coverage scoring catches these cross-experiment overlaps. See the "DONE category conflates" pitfall in the main SKILL.md for the full taxonomy.

## "DONE but partially open" triage pattern

When all queue items classify as DONE (source experiments completed) but many remain partially answered, the Director must triage each item to determine which warrant new tasks.

The classification taxonomy marks items DONE when their source experiment is in self_state.json — but this conflates "experiment answered the question" with "experiment touched the topic without addressing the specific question."

**Concrete triage steps:**
1. Extract all DONE items (source in self_state.json, not tagged RESOLVED)
2. Filter for items with `[ACTIVE — partial: ...]` tags or keywords like "need re-run", "unsolved", "not identified"
3. For each candidate, check if ANY running experiment already covers the question (keyword overlap against running task titles with coverage scoring)
4. Only create tasks for items with LOW coverage (<0.15) against running tasks
5. In Cycle #201, this pattern identified 7 genuinely open items from 37 DONE items — all had partial answers from completed experiments but specific sub-questions remained untested

**Real-world example (Cycle #201):**
- Queue had 42 items: 5 RESOLVED, 37 DONE, 0 RUNNING, 0 NO_SOURCE
- All 37 DONE items had source experiments in self_state.json
- 8 items had `[ACTIVE — partial: ...]` tags indicating incomplete investigation
- After coverage scoring against 16 running tasks, 7 items had LOW coverage (<0.15)
- 7 experiment tasks created, 1 worker left free

## Pre-creation topic-text cross-reference (added Cycle #253)

**The gap:** The queue item classification taxonomy (RESOLVED/RUNNING/DONE/NO_SOURCE) only checks whether an item's *source experiment* is running — not whether its *topic* is already being investigated by a different running experiment. This caused 3 duplicate tasks in Cycle #253: the curiosity scorer returned items like "Pre-flight classifier architectural alternatives" (source exp_1309, completed), but a running experiment (exp_1374) was already investigating the same question under a different experiment ID.

**Prevention pattern:** Before calling `kanban_create` for any queue item, run quantitative coverage scoring of the item's topic text against ALL running task titles. Only create if coverage is LOW (<0.15):

```python
import json, re

running = json.load(open('/tmp/kanban_running.json'))
running_titles = ' '.join(t.get('title', '').lower() for t in running)

stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'from'}

# For each candidate queue item:
item_text = "Pre-flight classifier architectural alternatives — non-linear routing for domain difficulty"
words = set(item_text.lower().split())
keywords = {w for w in words - stop if not re.match(r'^exp_\\d+\\w*$', w) and len(w) > 3}
overlap = sum(1 for w in keywords if w in running_titles)
coverage = overlap / max(len(keywords), 1)
# LOW <0.15 → safe to create, MED 0.15-0.3 → check manually, HIGH >0.3 → skip
```

**Key insight:** The existing "quantitative coverage scoring" section (Cycle #177) computes coverage of queue items against running task titles — but it was only used for queue triage during saturation, NOT as a gate before individual task creation. This pitfall promotes it to a mandatory pre-creation check.

**Real-world example (Cycle #253):**
- exp_1378 "Pre-flight classifier architectural alternatives" created for queue item (score 92)
- exp_1374 already running with title "Pre-flight classifier architectural alternatives — non-linear routing"
- Coverage score would have been >0.8 (HIGH) — should have been caught
- Result: 3 tasks created then immediately reclaimed+blocked as duplicates

## Semantic verification of queue coverage (added Cycle #245)

Keyword overlap scoring can miss cases where a queue item and running experiment investigate the *same question* using different vocabulary. Queue items come from synthesis findings (domain-specific language), while experiment titles come from the Director (technical language). The same question appears very different in these two contexts.

**After scoring, always verify manually:** "If this running experiment completes successfully, will it answer the queue item's specific question?"

**Concrete examples (Cycle #245):**

| Queue item | Running experiment | Keyword score | Semantic match | Action |
|:---|:---|:---:|:---:|:---|
| "2-feature INT1 achieves 100% at 2.3us — can this be deployed as universal first-pass filter?" | "INT1 standalone vs cascade — theoretical framework" | LOW (0.18) | **HIGH** — same question | SKIP |
| "Character calibration +69.6% ECE reduction — does this transfer to production?" | "Character calibration production transfer — ECE reduction on real user" | MED (0.22) | **HIGH** — same question | SKIP |
| "TF-IDF+LR standalone at 100+ domains?" | "Streaming 6-feature detector adversarial robustness" | LOW (0.12) | **LOW** — different question | CREATE |
| "Entropy_delta 96% importance — what multi-feature detector resists white-box?" | "Entropy_delta dominance creates single point of failure" | MED (0.25) | **HIGH** — same question | SKIP |

**Decision matrix:**

| Keyword score | Semantic match | Action |
|:---:|:---:|:---|
| HIGH (>0.3) | Any | SKIP |
| MED (0.15-0.3) | HIGH | SKIP |
| MED (0.15-0.3) | LOW | CREATE |
| LOW (<0.15) | HIGH | SKIP |
| LOW (<0.15) | LOW | CREATE |

**Outcome:** 16 active items classified, 12 covered (source running OR semantic match), 4 uncovered → 3 tasks created, 0 duplicates. All queue items covered after task creation.

## Recheck unsynthesis after recent synthesis

When multiple synthesis tasks have completed recently (within the last 30 minutes), recheck the unsynthesized count before creating a new synthesis task. Recent synthesis may have already covered experiments that appeared unsynthesized on the initial check.

```python
import json, re, os, time

done = json.load(open('/tmp/kanban_done.json'))
now = time.time()

# Find recent synthesis tasks (last 30 min)
recent_synth = []
for t in done:
    title = t.get('title', '').lower()
    completed = t.get('completed_at', 0)
    if ('synth' in title or 'consolidat' in title) and (now - completed) < 1800:
        recent_synth.append(t)

if recent_synth:
    print(f"Found {len(recent_synth)} recent synthesis tasks — rechecking unsynthesized count")
    # Recompute after self_state.json may have been updated
    ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
    ss_exp_ids = set()
    exps = ss.get('experiments', {})
    if isinstance(exps, dict):
        for e in exps.get('completed', []):
            eid = e.get('id', '') if isinstance(e, dict) else ''
            if eid: ss_exp_ids.add(eid)
    else:
        for e in ss.get('metrics', {}).get('experiments_completed_list', []):
            if isinstance(e, int):
                ss_exp_ids.add(f'exp_{e}')
            elif isinstance(e, str):
                m2 = re.search(r'exp_(\\d+)', e)
                if m2: ss_exp_ids.add(f'exp_{m2.group(1)}')
    
    done_exp_ids = set()
    for t in done:
        title = t.get('title', '')
        is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
        if not is_synth:
            for m in re.finditer(r'exp_(\d+\w*)', title):
                done_exp_ids.add(f'exp_{m.group(1)}')
    
    unsynth = done_exp_ids - ss_exp_ids
    print(f"Unsynthesized after recent synthesis: {len(unsynth)}")
```

**Real-world example (Cycle #201):**
- 3 recent synthesis tasks (completed within 14 minutes) had reduced unsynthesized count
- Initial check showed 2 unsynthesized (exp_606, exp_614)
- After verifying recent synthesis didn't cover them, created dedicated synthesis task
- Avoided creating redundant synthesis for already-consolidated experiments
