## Director synthesis — reading completed task summaries at scale

When running as Director with 10+ completed tasks, you need to read summaries efficiently. The naive approach (`hermes kanban show <id> --json | python3 -c "..."`) triggers TIRITH's `pipe_to_interpreter` on EVERY attempt. The reliable pattern:

**Two-step file-based approach (always works):**
```bash
# Step 1: Dump to file (no pipe — passes TIRITH)
hermes kanban show t_xxxxxxxx --json 2>&1 > /tmp/task_summary.json

# Step 2: Process file in a SEPARATE command (no pipe — passes TIRITH)
python3 -c "import json; d=json.load(open('/tmp/task_summary.json')); print(d.get('latest_summary','N/A'))"
```

**Batch reading pattern (for 5+ tasks):**
```bash
# CRITICAL: --json flag is REQUIRED. Without it, output is human-readable text, not JSON.
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json

# Process in a separate command (TIRITH blocks pipes)
# CRITICAL: hermes kanban list --json returns a JSON ARRAY, not {"tasks": [...]}
python3 << 'EOF'
import json
d = json.load(open('/tmp/kanban_done.json'))
# d is a list of task objects, NOT a dict with a "tasks" key
for task in d:  # NOT d.get('tasks', [])
    print(f"{task['id']}: {task.get('title', 'N/A')}")
EOF
```

**⚠ Summary availability gap:** `hermes kanban list --json` returns task objects that do NOT contain `latest_summary` — they only have `id`, `title`, `body`, `status`, etc. To get the completion summary, you MUST use `hermes kanban show <id> --json` for each individual task. For 5+ tasks, iterate and dump each to a separate file, then process in batch.

**⚠ `completed_at` format varies between endpoints (fixed Cycle #252).** The `completed_at` field can be a Unix epoch integer OR an ISO 8601 string depending on whether you used `kanban list --json` or `kanban show --json`. See `references/completed-at-format-variants.md` for the safe parser and full details. Code that assumes one format will fail on the other — use `parse_timestamp()` that handles both.

**⚠ `kanban show --json` nested structure:** The output wraps most fields under a `task` key. Top-level keys are: `task`, `latest_summary`, `parents`, `children`, `comments`, `events`, `runs`. To access `status`, `title`, `events` etc., navigate through the nested `task` object:
```python
d = json.load(open('/tmp/task_summary.json'))
status = d.get('task', {}).get('status', '?')       # NOT d.get('status')
title  = d.get('task', {}).get('title', 'N/A')      # NOT d.get('title')
events = d.get('task', {}).get('events', [])         # NOT d.get('events')
# But latest_summary IS at top level:
summary = d.get('latest_summary', 'N/A')
```

**⚠ `comments` can be `null`, not `[]`.** When a task has no comments, `kanban show --json` returns `"comments": null` — NOT an empty array. Iterating over `None` raises `TypeError: 'NoneType' object is not subscriptable`. Always use `or []` to coerce:
```python
comments = d.get('comments', []) or []  # SAFE — handles null
for c in comments:
    print(c.get('body', ''))
```

**Avoid:** Any `command | python3` pattern — TIRITH blocks it regardless of what the left-side command is (`hermes`, `cat`, `grep`, `head`). The filter matches on the pipe-to-interpreter structure, not the specific command.

**F-string backslash causes SyntaxError in heredocs (added Cycle #191).** When writing Python code inside `python3 << 'EOF' ... EOF` heredocs, f-strings containing backslash patterns (like `\d+` for regex) cause `SyntaxError: f-string expression part cannot include a backslash` on Python ≤3.11. This is a Python version limitation, not a TIRITH filter. The error is confusing because the f-string looks valid. **Fix**: Write the script to a file first using `write_file`, then execute it separately with `python3 /tmp/script.py`. Example that FAILS:
```bash
python3 << 'EOF'
import re
ids = {f'exp_{m.group(1)}' for m in re.finditer(r'exp_(\d+)', text)}  # ← SyntaxError
EOF
```
Example that WORKS: use `write_file` to create `/tmp/script.py`, then `python3 /tmp/script.py`. Alternatively, avoid f-strings with regex backslashes by using `%s` formatting or `.format()` instead. This pitfall is most common in Director passes that build sets of experiment IDs from task titles.

**Shell `&` misinterprets Python set operators in heredocs (added Cycle #152).** When writing Python code inside `python3 << 'PYEOF' ... PYEOF` heredocs, the shell interprets `&` as a background operator BEFORE the Python code runs. This breaks set intersection (`set_a & set_b`), bitwise AND, and any expression using `&`. The error is cryptic: `Foreground command uses '&' backgrounding. Use terminal(background=true)`. **Fix**: Use method syntax instead of operators:
```python
# BAD — shell interprets & as background operator
unsynthesized = done_exp_ids - ss_exp_ids
synthesized = done_exp_ids & ss_exp_ids   # ← BROKEN

# GOOD — method syntax avoids shell interpretation
synthesized = done_exp_ids.intersection(ss_exp_ids)
unsynthesized = done_exp_ids.difference(ss_exp_ids)
```
This applies to ALL Python set/dict operators in heredocs: `&` (intersection), `|` (union), `^` (symmetric difference). Use `.intersection()`, `.union()`, `.symmetric_difference()` instead.

## Director Synthesis workflow (concrete steps)

When running as Director with ≥7 running tasks, shift from task creation to synthesis. The synthesis cycle has 5 concrete steps:

### 1. Read completed task summaries
```bash
# --json flag REQUIRED for machine-readable output
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json
```
Then read individual summaries (list output does NOT include `latest_summary`):
```bash
hermes kanban show <task_id> --json 2>&1 > /tmp/summ_<id>.json
```
Process in a separate command (TIRITH blocks pipes).

**Identify unsynthesized experiments (added Cycle #148, refined Cycle #150, Cycle #151, Cycle #160, extended June 2026 — see `references/stale-done-list-unsynthesis-pitfall.md` for the stale done list pitfall):** Before creating the synthesis task, determine which experiments need synthesis. **DO NOT rely on extracting experiment IDs from synthesis task titles** — titles only mention a subset of covered experiments (e.g., "consolidate 67 unsynthesized experiments" lists none; "consolidate exp_178+180+181+186" misses the other 63). This produces false "unsynthesized" counts (75 found, most already covered). Instead, use one of these reliable methods:
- **Method A (recommended, simplest):** Compare done task IDs directly against `self_state.json`'s `experiments.completed` array. Extract experiment IDs from done task titles via regex (`exp_\d+`), then subtract the set of IDs already in `self_state.json`. The remainder are unsynthesized. This is the ground truth — if an experiment ID is not in `self_state.json`, it needs synthesis regardless of what synthesis task titles claim.
  ```python
  import json, re
  done = json.load(open('/tmp/kanban_done.json'))
  done_exp_ids = set()
  for t in done:
      title = t.get('title', '')
      # CRITICAL: Exclude synthesis tasks from extraction. Synthesis task titles
      # mention experiment IDs as subjects (e.g., "SYNTHESIS: consolidate exp_310,
      # exp_338, exp_321"), but the actual experiment task may still be running.
      # Including these IDs causes false positives: the experiment shows as "done"
      # (from the synthesis title) but isn't in self_state.json (because it hasn't
      # completed yet), so it's incorrectly flagged as unsynthesized.
      is_synth = 'synth' in title.lower() or 'consolidat' in title.lower()
      if not is_synth:
          for m in re.finditer(r'exp_(\d+)', title):
              done_exp_ids.add(f'exp_{m.group(1)}')
  ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
  ss_exp_ids = set()
  for e in ss.get('experiments',{}).get('completed',[]):
      if isinstance(e, dict):
          ss_exp_ids.add(e.get('id',''))
      elif isinstance(e, str):
          m = re.search(r'exp_(\d+)', e)
          if m:
              ss_exp_ids.add(f'exp_{m.group(1)}')
  unsynth = done_exp_ids - ss_exp_ids
  ```
  **Pitfall — mixed experiment ID formats break naive sorting (added Cycle #166):** Experiment IDs like `exp_104b` have letter suffixes that cause `int()` conversion to fail (`ValueError: invalid literal for int() with base 10: '104b'`). When sorting experiment IDs, use a regex-based sort key:
  ```python
  import re
  def sort_key(x):
      m = re.search(r'(\d+)', x)
      return int(m.group(1)) if m else 0
  sorted(ids, key=sort_key)
  ```
  This handles both `exp_123` and `exp_104b` correctly. Apply this pattern anywhere experiment IDs are sorted or compared numerically.
- **Method B (audit-trail based):** Read `self_state.json` → `audit_trail_synthesis` array. Each entry lists `experiments_synthesized`. Collect all IDs from this array — these are definitively covered. Subtract from done_exp_ids.
- **Method C (conservative):** If unsure, list ALL done experiments as potentially unsynthesized and let the synthesis worker deduplicate. The synthesis worker reads self_state.json first and only adds genuinely new entries.

After identifying unsynthesized experiments, **list their IDs explicitly in the synthesis task body** so the synthesis worker knows exactly what to process, rather than making it re-scan all 100+ done tasks. Pattern for the synthesis task body:
```
SYNTHESIS CYCLE: Read all completed Kanban tasks since last synthesis.
Experiments to synthesize: exp_XXX (one-line summary), exp_YYY (one-line summary).

After analysis, write your output:
python3 ~/.hermes/scripts/write_synthesis_output.py \
  --task-id $HERMES_KANBAN_TASK \
  --experiments "exp_NNN,..." \
  --resolutions '[{"item": N, "resolved_by": "exp_XXX", "note": "..."}]' \
  --curiosities "Follow-up 1?;Follow-up 2?"
This writes to the synthesis_outputs table. synthesis_merger.py (cron 2m) applies to self_state.json.

CRITICAL: Read self_state.json FIRST (source of truth). Do not run experiments. Write via write_synthesis_output.py — do NOT write to self_state.json directly.
```

### 2. Update self_state.json
Use Python file I/O (NOT `patch` — old_string matches are non-unique in 400KB+ JSON):
```python
import json, os
from datetime import datetime, timezone

path = os.path.expanduser('~/.hermes/self_state.json')
d = json.load(open(path))

# CRITICAL: Deduplicate experiments_completed_list before updating.
# Multiple synthesis cycles append the same experiment IDs, causing the list
# to grow unboundedly (observed: 101→97 after dedup in Cycle #146).
# Always deduplicate: list(dict.fromkeys(d['metrics']['experiments_completed_list']))
completed = list(dict.fromkeys(d['metrics'].get('experiments_completed_list', [])))
# Safety: also deduplicate d['experiments']['completed'] entries by ID
seen_ids = set()
deduped_experiments = []
for exp in d.get('experiments', {}).get('completed', []):
    eid = exp.get('id', '')
    if eid and eid not in seen_ids:
        seen_ids.add(eid)
        deduped_experiments.append(exp)
d['experiments']['completed'] = deduped_experiments

# Add new experiments to d['experiments']['completed'] (dict entries, not just IDs)
# Update d['metrics'] — ALL counters must use the SAME actual completed task count
# Update d['identity']['purpose'] with current phase summary
# Update d['last_updated'] and d['version']

d['metrics']['experiments_completed_list'] = completed
d['metrics']['experiments_completed'] = len(completed)
d['metrics']['experiments_conducted'] = len(completed)
d['metrics']['experiments_completed_count'] = len(completed)
d['version'] = d.get('version', 0) + 1
d['last_updated'] = datetime.now(timezone.utc).isoformat()

with open(path, 'w') as f:
    json.dump(d, f, indent=2, ensure_ascii=False)
```

### 2b. Sync to SQLite (dashboard data source)
After writing self_state.json, run the sync script to push new experiments to SQLite (which the dashboard reads):
```bash
python3 ~/.hermes/scripts/sync_experiments_to_db.py
```
This inserts any experiments in self_state.json that are missing from prometheus.db. The dashboard auto-refreshes every 10s and will pick up new experiments immediately. **This step is MANDATORY** — without it, the dashboard shows stale data.

### 3. Curate the queue
- **Tag resolved items during synthesis (Step 2) BEFORE curation.** When an experiment completes and answers a queue item, prepend `[RESOLVED by exp_XXX]` to the item's text. Without this tag, curation cannot distinguish resolved from unresolved items. This must happen in Step 2 (self_state update) — by Step 3, you're reading the queue for filtering, not writing to it.
- Remove items tagged `[RESOLVED]` or covered by running/completed experiments
- De-duplicate items asking the same question (automated via `curiosity_scorer.py` `is_already_in_queue()` at scoring time; manual pass for edge cases)
- Add 1-3 new curiosities from synthesis findings
- Keep ≥5 items as buffer (running tasks may fail)

### 4. Update knowledge graph
Add new subtopics to relevant domains in `d['knowledge_graph']['domains']`.

### 5. Log to audit trail
Append to `~/.hermes/self_audit.log` with timestamp, experiments synthesized, and self-modifications made.

**Metrics counter drift (added Cycle #152, extended Cycle #159, escalated Cycle #190, Cycle #218):** The `experiments.completed` array length can diverge from the metrics counters (`experiments_completed`, `experiments_conducted`, `experiments_completed_count`) when a synthesis worker updates the array but doesn't fully reconcile the counters. **Progression of observed drift:** Cycle #152: 3-entry drift (200 vs 197). Cycle #159: 3-entry drift (270 vs 267). **Cycle #190: 163-entry drift (480 vs 317)** — this is a catastrophic escalation showing the drift is cumulative and unbounded. **Cycle #218: 35-entry drift (751 vs 716)** — drift persists despite documented prevention. The synthesis code above sets all counters to `len(completed)` which is correct, but if a prior synthesis only updated the array (appending new experiments) without updating the counters, the drift accumulates. **Diagnostic**: When reading self_state.json, compare `len(experiments.completed)` against `metrics.experiments_completed`. If they differ, the counters are stale — the synthesis worker should use the array length as the authoritative count. This is the same "single source of truth" principle: the array is ground truth, counters are derived. **Prevention**: Every synthesis worker must set ALL counter fields to `len(completed)` in a single atomic write, not just append to the array. **Director action when drift detected**: Create a dedicated SYNTHESIS task (not bundled with experiment synthesis) whose PRIMARY objective is counter reconciliation. The task body should explicitly state the current drift values and instruct the synthesis worker to set all counters to `len(completed)`. This ensures the reconciliation gets dedicated attention rather than being a secondary concern in a larger synthesis pass. **Concrete anti-drift pattern**: See `references/synthesis-counter-reconciliation.md` for the full atomic reconciliation code with verification step. Include concrete counter values in synthesis task bodies (not just "set all counters to len(completed)") so synthesis workers have a concrete verification target.

**CRITICAL FORMAT RULE (added Cycle #148):** The Synthesis worker writes `CYCLE #N` entries. Directors write `DIRECTOR PASS` entries (no cycle number). Never write `CYCLE #N` from a Director pass — this causes cycle counter drift from multiple passes reading stale state. See `director-loop` SKILL.md for full details.

**Director Pass audit log template:** See `references/director-pass-audit-template.md` for the structured format. Every Director pass should append a Board/Workers/Actions/Coverage/Next-cycle entry to `~/.hermes/self_audit.log`.

**Queue item resolution technique (added Cycle #151):** When curating the queue as Director, resolve items by cross-referencing their source experiment against `self_state.json`'s `experiments.completed` array. Each queue item typically references a source experiment (`[NEW from exp_XXX]`). If that source experiment IS in `self_state.json` and its findings answer the queue item's question, the item is resolved. For each queue item:
1. Extract experiment reference via regex: `re.findall(r'exp_(\d+)', item_text)`
2. Check if each referenced experiment is in `self_state.json`'s `experiments.completed`
3. Read the experiment's findings (knowledge graph entries, purpose field updates) to determine if the queue item's question was answered
4. **DO NOT tag items in self_state.json** — instead, note which items should be tagged `[RESOLVED exp_XXX]` and include this in a `kanban_comment` on the synthesis task (see `references/director-queue-curiosity-pitfall.md`)
5. For items whose source experiment IS in self_state but the question remains open (e.g., follow-up not yet tested), note as ACTIVE in the comment
6. For items where the source experiment is NOT in self_state, the item is from a yet-to-be-synthesized experiment — keep as active

This produces a clean queue where every item is explicitly tagged as ACTIVE or RESOLVED, making the curation pass deterministic rather than requiring the Director to re-read experiment summaries. Use the two-step file pattern for reading self_state.json (TIRITH blocks `cat | python3` pipes).

**Pitfall — synthesis title/summary claims don't match actual coverage (added Cycle #159, extended Cycle #214):** A synthesis task's title AND summary can both claim consolidation of experiments that are NOT in `self_state.json`'s `experiments.completed` array. In Cycle #214, synthesis t_ecdbb6b1 summary explicitly stated "Consolidated exp_733" but exp_733 was absent from self_state.json. Do NOT trust synthesis titles OR summaries — only `self_state.json` is ground truth. See `references/synthesis-coverage-verification.md` for the full verification pattern.

**Pitfall — partial experiment completion (added Cycle #188).** A task can reach `status=done` with a summary that explicitly says "PARTIAL" — some domains/questions succeeded while others failed (e.g., blocked by security scanner, API errors, timeout on specific domains). The task IS done (the worker called `kanban_complete`), but the results only cover a subset of the intended scope. **Detection**: Read the `latest_summary` or `runs[].summary` for keywords like "PARTIAL", "incomplete", "only N/M domains", "failed on". **Impact on synthesis**: The synthesis worker must note which parts are covered and which are gaps — partial results should NOT be treated as complete coverage. **Impact on queue**: A queue item whose source experiment produced partial results should NOT be tagged RESOLVED unless the partial results specifically answer the question. If the question spans domains that were NOT tested, keep as ACTIVE. **Prevention**: Worker summaries should explicitly state scope coverage (e.g., "PLATFORM complete (9/9 questions), other 4 domains failed"). The Director should check for "PARTIAL" in summaries before assuming full coverage.

**Pitfall — premature queue item resolution (added Cycle #155):** Don't tag a queue item as RESOLVED just because the source experiment completed. You MUST verify the experiment actually answered the specific question asked. In Cycle #155, exp_295 ("Does contradictory-fact catastrophe hold across PLATFORM, DISPATCH?") was tagged RESOLVED because exp_295 completed — but exp_295 only tested DICT, not the cross-domain question. The item was correctly re-tagged as ACTIVE because exp_321 (still running) was testing the cross-domain case. **Rule**: Before tagging RESOLVED, read the experiment summary and confirm it addresses the queue item's question scope, not just its topic. If the experiment tested a subset of the question's scope, keep as ACTIVE with a note about which aspect is still open.

**Runnable script:** `scripts/director_queue_triage.py` runs the full classification + coverage scoring + unsynthesis detection in one command. Dump board state first (`hermes kanban list --status {running,done} --json > /tmp/kanban_{running,done}.json`), then run the script.

**Queue item classification taxonomy (added Cycle #184, refined Cycle #187):** Before creating tasks, classify every queue item into one of these categories. This determines what needs new tasks vs what's already covered:

```python
import json, re, os

# Load state
running = json.load(open('/tmp/kanban_running.json'))
running_exp_ids = set()
for t in running:
    for m in re.finditer(r'exp_(\d+\w*)', t.get('title', '')):
        running_exp_ids.add(f'exp_{m.group(1)}')

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])
ss_exp_ids = set()
for e in ss.get('experiments', {}).get('completed', []):
    eid = e.get('id', '') if isinstance(e, dict) else (re.search(r'exp_(\d+\w*)', e).group(0) if re.search(r'exp_(\d+\w*)', e) else '')
    if eid: ss_exp_ids.add(eid)

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'RESOLVED' in text: continue  # already tagged
    source_ids = re.findall(r'exp_(\d+\w*)', text)
    if not source_ids:
        category = "NO_SOURCE"   # Pure research question, no experiment reference
    elif any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
        category = "RUNNING"     # Source experiment still running — will resolve naturally
    elif any(f'exp_{sid}' in ss_exp_ids for sid in source_ids):
        category = "DONE"        # Source experiment completed but item not yet tagged RESOLVED
    else:
        category = "UNKNOWN"     # Source experiment not found anywhere
    print(f"[{i}] ({category}) {text[:100]}")
```

**Categories:**
- **RESOLVED**: Already tagged by synthesis worker. Skip.
- **RUNNING**: Source experiment is still running. Do NOT create a task — it will resolve when the experiment completes and synthesis processes it. (This is the "Queue cross-referencing" pattern from Cycle #154.)
- **DONE**: Source experiment is completed but the queue item hasn't been tagged RESOLVED yet. These are candidates for (a) tagging RESOLVED if the experiment answered the question, or (b) creating a follow-up task if the question is broader than what the experiment tested.
- **NO_SOURCE**: Pure research question, always a candidate — but check synthesis coverage first (see `references/synthesis-covered-no-source-items.md`).
- **UNKNOWN**: Source experiment not found in self_state or running tasks. May need investigation.

**Pitfall — DONE category conflates "answered" vs "touched topic" (added Cycle #187):** The classification marks an item as DONE whenever its source experiment is in `self_state.json` — but this conflates two very different situations: (1) the experiment actually answered the queue item's question (should be tagged RESOLVED), and (2) the experiment just touched the same topic without addressing the specific question (still a candidate for new tasks). In Cycle #187, items 43-49 were all classified as DONE_NOT_COVERED even though their source experiments (exp_512-526) had been synthesized — the classification couldn't distinguish "RichCompare checker recall improvement" (item 47, experiment built a checker but didn't improve recall) from "RichCompare value contradiction" (item 20, experiment confirmed the mechanism). **Fix**: After classifying DONE items, the Director must check whether the source experiment's findings actually address the queue item's specific question. If yes, tag as `[RESOLVED by exp_XXX]`. If the experiment only partially addressed the question or tested a different aspect, keep as `[ACTIVE — partial: <what's still open>]`. Only create new tasks for ACTIVE items whose question is genuinely uncovered.

**Pitfall — NO_SOURCE items can overlap topically with running experiments (added Cycle #192).** The classification marks items as NO_SOURCE when they lack an `exp_NNN` source ID — but this doesn't mean the topic is uncovered. A queue item like "FDA counter-fact 10pp vulnerability — is this domain-specific?" has no source ID, yet exp_529 (running) is investigating exactly that question. The keyword-based coverage scoring (Cycle #177) catches these overlaps: run it on NO_SOURCE items to filter out those already covered by running tasks. In Cycle #192, 4 NO_SOURCE items were found but only 1 was genuinely uncovered after coverage scoring — the other 3 shared significant keywords with running experiment titles. **Rule**: After classifying NO_SOURCE items, run quantitative coverage scoring against running task titles. Only create tasks for NO_SOURCE items with LOW coverage (<0.15). This prevents the most common waste from the NO_SOURCE category: creating tasks for topics that running experiments will resolve.

**Decision rule:** Only create new tasks for NO_SOURCE items (with LOW coverage after scoring) and DONE/ACTIVE items whose question is NOT fully answered. RUNNING items should be left alone. Always verify DONE items before creating tasks — the classification alone is insufficient. See `references/director-queue-triage-cycle201.md`, `references/director-zero-candidate-queue.md`, and `references/unsynthesized-source-deferred-items.md` (deferring items whose source experiments are unsynthesized).

**When to create new tasks vs synthesize:**
- ≥7 running: SYNTHESIZE only — do NOT create new experiment tasks (creating more tasks during saturation wastes cycle time on assessment overhead). However, creating a SYNTHESIS task is maintenance, not investigation — always create one when unsynthesized experiments exist, regardless of saturation.
- <7 running: create tasks for uncovered queue items, then synthesize
- **Pitfall — creating tasks for RUNNING queue items wastes workers (added Cycle #186).** When classifying queue items, the most common mistake is creating a task for an item whose source experiment is still running. The item WILL resolve naturally when the running experiment completes and synthesis processes it. Creating a duplicate task wastes a worker slot. **Detection**: Always run the queue classification code (RESOLVED/RUNNING/DONE/NO_SOURCE/UNKNOWN) before creating tasks. Only create tasks for NO_SOURCE and DONE items whose question is NOT fully answered. **Priority order for task creation**: (1) NO_SOURCE items (pure research, always valuable), (2) DONE/LOW-coverage items (source completed, question open), (3) DONE/MED-coverage items (partial coverage, lower priority). Never create tasks for RUNNING items.

**Pitfall — status desync inflates saturation count (added Cycle #185).** `kanban list --status running --json` may include tasks that are actually `done` (status desync, documented above). This inflates the running count, potentially pushing the Director above the ≥7 saturation threshold when the true count is lower. In Cycle #185, the list showed 7 running tasks, but one (exp_506/t_0d4b7ddd) was actually done — the true count was 6, which is below the threshold and would have allowed creating new experiment tasks. The Director incorrectly applied synthesis-only mode and missed an opportunity to dispatch 24 active queue items. **Fix**: When the running count is near the threshold boundary (6-8), spot-check ambiguous tasks with `kanban show <id> --json` to verify actual status before applying the saturation decision. Use the verified count, not the list count.
- Queue >50 items: run curation pass as the investigation task for that cycle. Use `queue_cleanup.py --apply` (in `~/.hermes/scripts/`) for automated cleanup — it removes resolved items, deduplicates, and caps at 50.
**SYNTHESIS AND CURATION TASKS ARE MAINTENANCE, NOT INVESTIGATION (added Cycle #184, extended Cycle #185).** The saturation threshold ("≥7 running: SYNTHESIZE only") gates EXPERIMENT tasks, not maintenance tasks. Creating a synthesis task for unsynthesized experiments AND a curation task for queue cleanup is maintenance work that should happen regardless of saturation. If you find 3+ unsynthesized experiments AND queue >50 items during a saturated Director pass, create BOTH maintenance tasks in the same pass — they are independent and can run in parallel. The correct pattern: (1) identify maintenance needs, (2) create tasks assigned to appropriate workers (prometheus-synthesis for synthesis, a free worker for curation), (3) dispatch, (4) do NOT create experiment tasks.
- **Free workers during saturation (added Cycle #166):** Having free workers (e.g., 20/22 running, 3 free) does NOT override the saturation threshold. Free workers are picked up by the next dispatch tick for ready tasks — the Director should not create tasks just because slots exist. At 20+ running tasks, the assessment overhead of reading the queue, cross-referencing running experiments, and mapping dependencies exceeds the value of 1-2 additional experiments. Stay in synthesis mode. The free workers will be utilized naturally when current tasks complete and new ready tasks are dispatched.
- **Saturation + synthesis complete (added Cycle #158):** When ≥7 tasks are running AND the synthesis task has already completed (no unsynthesized experiments remain), the Director should still avoid creating new tasks unless there are genuinely independent, high-value queue items with free workers. The saturation threshold exists to prevent assessment overhead — even with synthesis done, creating tasks requires reading the queue, cross-referencing against running experiments, and mapping dependencies. If the queue is well-covered by running experiments, log the pass and chain to the next cycle. Only create new tasks during saturation when: (a) there are 3+ genuinely OPEN queue items not covered by any running experiment, AND (b) at least 3 workers are free.

**Queue triage during saturation (added Cycle #156):** When ≥7 tasks are running and you're in synthesis-only mode, quickly assess queue coverage to understand what's in-flight vs what's genuinely unassigned. This tells you whether the queue is well-covered or has gaps that need attention when saturation clears. Technique:
```python
import json, re

running = json.load(open('/tmp/kanban_running.json'))
running_titles = [t.get('title', '') for t in running]

ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    # Quick keyword match: do any running task titles share significant words with this item?
    covered = any(
        any(word.lower() in t.lower() for word in text.split()[:5])
        for t in running_titles
    )
    status = "COVERED" if covered else "OPEN"
    print(f"[{i}] ({status}) {text[:90]}")
```
This produces a quick triage: COVERED items will resolve when their running experiment completes and synthesis processes it. OPEN items are candidates for new tasks when saturation clears. Use the more precise "Queue cross-referencing" technique (Cycle #154) when actually deciding whether to create new tasks — this triage is for situational awareness only.

**Quantitative coverage scoring (added Cycle #177):** For a more granular triage than binary COVERED/OPEN, compute a coverage score per queue item based on keyword overlap with running task titles. Filter out common stop words first, then count what fraction of the item's keywords appear in any running title:
```python
stop = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'it', 'of', 'in',
        'to', 'for', 'with', 'on', 'at', 'by', 'from', 'as', 'what', 'how',
        'why', 'not', 'or', 'and', 'but', 'if', 'then', 'than', 'when',
        'which', 'who', 'we', 'you', 'they', 'new', 'from'}
# Also strip experiment ID prefixes — "exp_430" in queue items won't match
# running task titles reliably and adds noise to the coverage score
keywords = {re.sub(r'^exp_\d+\w*$', '', w) for w in words - stop if not re.match(r'^exp_\d+\w*$', w)}
running_titles_text = ' '.join(t.get('title', '').lower() for t in running)
overlap = sum(1 for w in keywords if w in running_titles_text)
coverage = overlap / max(len(keywords), 1)
# HIGH >0.3 (likely covered), MED 0.15-0.3 (partially covered), LOW <0.15 (open)
```
This catches partial matches that binary keyword checking misses — e.g., a queue item about "erosion on PLATFORM" partially matches a running task about "erosion on DISPATCH" (shared keyword "erosion"). Use HIGH/MED/LOW to decide whether to create new tasks: only create for LOW-coverage items during saturation.

**Queue cross-referencing before task creation (added Cycle #154):** Before creating new tasks, cross-reference running experiment IDs against queue item sources to avoid duplicate work. Many queue items are generated as follow-ups from experiments that are STILL RUNNING — creating a new task for these wastes a worker on work that will be covered when the running experiment completes and its synthesis resolves the queue item. Technique:
```python
import json, re

# Load running tasks
running = json.load(open('/tmp/kanban_running.json'))
running_exp_ids = set()
for t in running:
    running_exp_ids.update(re.findall(r'exp_(\d+)', t.get('title', '')))

# Load queue
ss = json.load(open(os.path.expanduser('~/.hermes/self_state.json')))
queue = ss.get('curiosity_queue', [])

# Find queue items whose source experiment is still running
for item in queue:
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    source_ids = re.findall(r'exp_(\d+)', text)
    if source_ids and any(f'exp_{sid}' in running_exp_ids for sid in source_ids):
        # This item's source experiment is still running — skip
        continue
    # Item is genuinely unassigned — candidate for new task
```
This prevents the common mistake of creating tasks for items like "exp_308 found X — does it generalize to Y?" when exp_308 is still running. The item will be resolved naturally when exp_308 completes and synthesis processes it. Only create tasks for items whose source experiments are DONE or whose questions are independent of any running experiment.

**Saturation threshold edge case (added Cycle #152):** OLD tasks (>80m, producing output) near completion — creating new tasks for free workers is acceptable even above threshold. **Mid-pass re-check:** After ANY task completes during a Director pass, re-check worker count and coverage scoring — board state is dynamic. See `references/mid-pass-worker-liberation.md`.

**Pre-run script skip threshold does NOT block synthesis (added Cycle #152).** When the pre-run data-collection script reports "SKIP: self_state.json updated <N>s ago (< 90s threshold)", this gates the FULL INVESTIGATION CYCLE (Steps 3-8 of the director-loop). It does NOT prevent creating synthesis tasks — synthesis is maintenance, not investigation. A recently-completed synthesis (updating self_state.json) means there may be NEW unsynthesized experiments that need consolidation. The correct Director behavior on a "skip" tick:
1. Still check the board for unsynthesized experiments (done tasks vs self_state.json)
2. Still create synthesis tasks if unsynthesized experiments exist
3. Do NOT create new experiment tasks (that's the investigation cycle the skip threshold gates)
4. Log the pass as `DIRECTOR PASS` with note that investigation was skipped but synthesis was dispatched

**Comment race condition — synthesis completes before comment arrives (added Cycle #188):** When you add an experiment to a running synthesis task via `kanban_comment`, the synthesis task may complete BEFORE the comment is processed. In Cycle #188, exp_530 was added to synthesis task t_66c61c6a via `kanban_comment`, but the task had already completed — the synthesis worker never saw the comment and exp_530 was missed. **Detection**: After adding a comment to a synthesis task, immediately check `kanban show <id> --json` for status. If status=done, the comment was too late. **Fix**: Create a new synthesis task for the missed experiment rather than trying to re-open the completed one. **Prevention**: Synthesis workers complete in **<2 minutes** — far faster than previously documented. A synthesis task with 0 heartbeats and <2 min age is NOT safe to comment on. The only reliable signal is `kanban show --json` reporting `status=done`. **Revised rule (Cycle #246, refined):** After dispatching a synthesis task, check the worker's event count. 0 events = safe to `kanban_comment` additional experiments. 1+ events = create separate synthesis task. See `references/synthesis-scope-expansion-0-events-check.md` for the full pattern.

**Late-completing experiments (added Cycle #147):** When experiments complete AFTER the synthesis task is already dispatched and running, add them to the synthesis task via `kanban_comment` rather than creating a second synthesis task. The synthesis worker reads the comment thread when it starts processing. Pattern:
```bash
hermes kanban comment <synthesis_task_id> "ADDITIONAL: Also synthesize exp_XXX and exp_YYY: <one-line summaries>"
```
This avoids duplicate synthesis tasks competing for the same self_state.json write. See `references/director-synth-comment-race-prevention.md` for the full status-check-before-commenting pattern.

**Expanding scope on a running synthesis task (added Cycle #159):** When the Director discovers unsynthesized experiments that the running synthesis task doesn't list in its body, use the same `kanban_comment` pattern to add them. Do NOT create a second synthesis task. The synthesis worker reads the comment thread and will include the additional experiments. Example: synthesis task body says "consolidate exp_332" but unsynthesized detection found exp_321 and exp_354 too — comment with "ADDITIONAL: Also synthesize exp_321 (...) and exp_354 (...)" and the synthesis worker handles it.

**⚠ Exception — second synthesis for non-overlapping remainder (added Cycle #245):** When the running synthesis is already actively processing (1+ events, heartbeats) and covers only a SUBSET of unsynthesized experiments, creating a second synthesis task for the remaining experiments is valid — the comment may arrive too late (race condition, Cycle #188). The key distinction: `kanban_comment` expands the SAME synthesis task's scope; a second synthesis task covers a DIFFERENT, non-overlapping set. See `references/second-synthesis-task-pattern.md` for the decision criteria and worked example.

**Pitfall — duplicate synthesis task in same Director pass (added Cycle #161, remedy added Cycle #162):** When the Director creates a synthesis task and dispatches it, then runs additional checks (queue triage, etc.) before the synthesis worker has started, it's easy to forget the synthesis task already exists and create a SECOND one for the same experiments. This wastes a worker slot and risks concurrent writes to self_state.json. **Prevention**: After creating a synthesis task, immediately record its task_id in a local variable or note. Before creating ANY new synthesis task, check `hermes kanban list --status ready --status running --json` for existing synthesis tasks (filter by title containing "synthesis" or "consolidat"). If one already exists and covers the same experiments, use `kanban_comment` to expand its scope instead of creating a duplicate. The two-step file pattern for reading the list output avoids TIRITH pipe blocks. **Remedy**: If duplicates are already running (both in `running` status), reclaim the less-progressed one: `hermes kanban reclaim <task_id>`. Check progress by comparing heartbeat count and age from `kanban show --json` — reclaim the newer task (less time running) or the one with fewer heartbeats. The reclaimed task returns to `ready` and won't waste a worker slot on the next dispatch tick.

### Worker Liveness Check

Before deciding whether to create new tasks or synthesize, verify that running workers are actually alive — status=running doesn't mean the worker is progressing. Two methods:

**Method 1: Process check (fast, reliable — added Cycle #145)**
```bash
ps aux | grep hermes | grep kanban
```
A worker process with the task ID in its command line is alive. This is the ground truth for liveness — if the process exists, the worker is running. Combine with heartbeat events for a complete picture (ps aux = process alive, heartbeat = process progressing).

**Pitfall — `pgrep -f` gives false negatives on macOS (added 2026-05-31).** Using `pgrep -f <task_id>` to check worker liveness can return no match even when the process is alive. The issue: `pgrep -f` matches against the full command line, but hermes agent processes launch via nested shell wrappers (`/bin/bash -lic set +m; cd ... && python3 ...`) where the task ID may not appear in the outer process's argv. `pgrep -af` (adding `-a` to print the full command line) fixes this. However, `ps aux | grep <task_id>` remains the most reliable approach since it searches the entire command string. **Rule**: For worker liveness checks, always use `ps aux | grep <task_id>` (Method 1) rather than `pgrep -f`. If you must use pgrep, always add the `-a` flag. A false-negative liveness check could cause the Director to incorrectly reclaim a live, healthy worker.

**Method 2: Heartbeat events (detailed, shows progress)**
Use the `events` array from `kanban_show --json`:

```bash
# Dump task details to file (TIRITH-safe)
hermes kanban show t_xxxxxxxx --json 2>&1 > /tmp/task_detail.json

# Check heartbeat recency in a SEPARATE command
python3 << 'EOF'
import json, datetime
d = json.load(open('/tmp/task_detail.json'))
events = d.get('task', {}).get('events', [])          # nested under 'task'
heartbeats = [e for e in events if e.get('kind') == 'heartbeat']
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
if heartbeats:
    last_hb = max(e.get('created_at', 0) for e in heartbeats)
    age_min = (now - last_hb) / 60
    print(f"Last heartbeat: {age_min:.0f}m ago, {len(heartbeats)} total")
else:
    print("NO heartbeats — worker may be stuck or just spawned")
EOF
```

**Interpreting results:**
- **<5 min ago**: Worker is actively running. Leave it alone.
- **5-15 min ago**: Worker may be doing a long computation (API calls, heavy I/O). Check if it has completion events — if not, it's likely still working.
- **>15 min ago with no completions**: Worker may be stuck. Check if it has `outcome: "timed_out"` in prior runs. If stuck, consider blocking the task with a reason so the operator can investigate.
- **No heartbeats at all**: This has TWO interpretations depending on what the worker is running:
  - **If the worker dispatched a hermes agent** (`hermes -p prometheus-worker-N chat -q work kanban task ...`): The hermes process sends heartbeats. No heartbeats means it just spawned or crashed. Check for `spawn_failed` events.
  - **If the worker is running a Python script directly** (`python3 exp_XXX.py`): The script does NOT send heartbeats — heartbeats are a hermes-agent feature, not a Python feature. **0 heartbeats is NORMAL for healthy script workers.** The process may have been running for 40+ minutes with zero heartbeats and be perfectly healthy. Use `ps aux` (Method 1) as ground truth for these workers — if the Python process is alive, the worker is alive. See Cycle #151 where all 15 running workers showed 0 heartbeats because they were all running Python scripts.

**Status desync between `kanban list` and `kanban show` (added Cycle #145, extended Cycle #161, Cycle #180):** Tasks can show `status=running` in `hermes kanban list --json` but actually be `status=done` when checked with `hermes kanban show <id> --json`. The list view doesn't update status in real-time after `kanban_complete`. Also, `kanban block` will fail with "cannot block" for tasks that are already done. **Diagnostic**: When a task shows 0 events and "cannot block", check `kanban show --json` for the actual status before assuming it's stuck. The summary in `latest_summary` or `runs[].summary` confirms completion. This desync cost a diagnostic cycle in exp_178/exp_181 before the true status was found. **Extended diagnostic (Cycle #161, revised Cycle #180):** A completed task MAY still have live OS processes — the hermes agent process does NOT always exit immediately after `kanban_complete`. In Cycle #180, 9 completed tasks had live processes (`ps aux` confirmed) lingering 1-2+ hours after their tasks were marked done. These are zombie processes: the kanban status is authoritative (`done`), but the hermes process hasn't fully exited. **Do NOT treat these as stuck workers** — the task is complete. If the zombie processes consume significant resources, they can be killed (`kill <PID>`), but they're harmless otherwise. The correct liveness check for completed tasks: (1) `kanban show --json` reports status=done with a summary → task is done, regardless of process state, (2) `ps aux` showing a live process for a done task is a zombie, not evidence of ongoing work. This is distinct from a crashed worker (which shows 0 processes AND status=running in `kanban show` with no completion summary).

**Stuck Worker Protocol (added Cycle #143, refined Cycle #145, extended Cycle #150+, Director-facing clarification Cycle #154, escalation pattern added Cycle #158):** When a worker shows zero events, diagnose BEFORE blocking — the cause matters. **Director note**: This protocol applies to Director reclaim decisions too. See `references/director-stuck-task-protocol.md` for the Director-specific decision flowchart and the recurring stuck task escalation pattern.

1. **Check process liveness** (`ps aux | grep hermes | grep <task_id>`):
   - Process NOT found → crashed. Block immediately: `"stuck: process dead, 0 events in {N}m — needs re-dispatch"`
   - Process FOUND → alive. May be in a long API call (reasoning models: 60-120s/call; multi-model evals: 10-30min). Do NOT block yet — but CHECK CPU time and workspace output first.
   **CPU time diagnostic (added after exp_448, refined Cycle #218):** Check the SUBPROCESS's cumulative CPU time, not the hermes agent's. When a worker dispatches a Python script via `terminal(background=true)`, the hermes agent has low CPU (it's waiting), but the subprocess is what matters. See `references/subprocess-cpu-time-diagnostic.md` for the full pattern. Quick version: `ps aux | grep '<script_name>.py'` to find the subprocess, then `ps -p <pid> -o time` to check CPU. A subprocess alive 30+ min with <1s CPU is HUNG.
2. **Check workspace output** (new in Cycle #150+): Even with process alive, inspect the workspace:
   - `ls -la ~/.hermes/kanban/workspaces/<task_id>/` — count files
   - If workspace contains ONLY the original script file and NO output/results files after 60+ minutes → process is hung (likely stuck on an API call that never returns, or blocked on I/O). Block with reason noting the zero-output pattern.
   - If workspace has output files being written → process is working. Leave alone.
   - **⚠ tee-output blind spot:** Scripts that pipe output via `tee /tmp/exp_XXX_output.log` write to `/tmp/`, NOT the workspace. The workspace will show only the original script file — a false "hung" signal. When workspace shows 1 file but process is alive, also check `ls -lt /tmp/exp_*_output.log 2>/dev/null | head -3` and `ls -lt ~/.hermes/experiments/exp_*_output.log 2>/dev/null | head -3` for external output. See "Blind spot — output logs outside workspace" under Method 3.
3. **Assess duration since last signal:**
   - <30m, alive → normal long computation. Leave alone.
   - 30-60m, alive → unusual but possible for API experiments. Log concern, don't block.
   - 60-120m, alive, 0 heartbeats AND zero workspace output → likely hung. Block.
   - 60-120m, alive, 0 heartbeats BUT workspace has output files → long computation, leave alone.
   - >120m, alive, 0 heartbeats → almost certainly stuck. Block.
3. **Block if stuck** with specific reason. Do NOT re-create immediately — underlying issue may recur. Let operator diagnose.
4. **Continue with other tasks** — one stuck worker shouldn't stall the pipeline.
5. **Log the stuck worker** in audit trail for pattern tracking.

**Real-world example (Cycle #145):** exp_158 ran 122m with 0 heartbeats but `ps aux` showed the worker process running a Qwen3.6 API call. Worker was NOT stuck — executing a long multi-model evaluation. Blocking would have wasted existing API calls. Process-alive check prevented false positive. See `references/stuck-worker-diagnosis.md` for the full decision flowchart.

**Real-world example (Cycle #150+):** exp_214 ran 82m with 0 heartbeats. `ps aux` showed the Python process alive (PID 81940, running since 4:20AM). But workspace inspection revealed only 1 file — the original `exp_214_dispatch_fact_minimization.py` script — with zero output files and last modification at 4:17AM (81m stale). This is a HUNG process: alive but producing nothing. Blocked with reason noting zero-output pattern. Contrast with exp_230 (55m, process alive, workspace SLOW with 5 files being written) which was healthy.

**Method 3: Workspace file modification time (fastest, supplementary — added Cycle #145)**

Check whether the worker's workspace is being actively modified. Active file writes indicate the worker is alive and progressing, even if heartbeats are absent.

```python
import os, time

workspaces = '/Users/future/.hermes/kanban/workspaces'
for tid in running_task_ids:
    ws = os.path.join(workspaces, tid)
    if not os.path.isdir(ws):
        print(f'{tid}: NO WORKSPACE')
        continue
    files = os.listdir(ws)
    latest_mtime = max(
        (os.path.getmtime(os.path.join(ws, f)) for f in files if os.path.isfile(os.path.join(ws, f))),
        default=0
    )
    age_min = (time.time() - latest_mtime) / 60 if latest_mtime > 0 else 999
    status = 'ACTIVE' if age_min < 5 else ('SLOW' if age_min < 15 else 'STALE')
    print(f'{tid}: {status} (last mod {age_min:.0f}m ago, {len(files)} files)')
```

**Interpreting results:**
- **ACTIVE (<5m)**: Worker is writing files — actively running experiments. Leave alone.
- **SLOW (5-15m)**: Worker may be in a long API call or between write phases. Check `ps aux` to confirm process is alive.
- **NEWLY DISPATCHED (0 files, 999m age)**: Task was just spawned. The worker process hasn't started writing files yet. This is NORMAL — do NOT diagnose as stuck. Cross-reference with `ps aux` to confirm the process exists. Wait 5+ minutes before checking again. See pitfall below.
- **STALE (>15m)**: Two possibilities:
  - **Process alive** (`ps aux` confirms) → worker is in a long API call (reasoning models: 60-120s/call; multi-model evals: 10-30min). NOT stuck. Leave alone.
  - **Process dead** (`ps aux` shows nothing) → worker crashed. Block and re-dispatch.

**Pitfall — STALE workspace false positive (added Cycle #145):** Workspace modification time shows "STALE" (>15m) when the worker is actually healthy but executing a long API call (e.g., multi-model evaluation with 60-120s per call). The workspace files aren't being written during API waits. **Always cross-reference with `ps aux`** before concluding a STALE worker is stuck. In Cycle #145, two workspaces showed 17m and 29m stale but `ps aux` confirmed both processes were alive and actively running API calls. The output logs (`/tmp/exp_XXX_output.log`) showed steady progress with retries — the workers were healthy.

**Pitfall — "Alive but silent" hung process (added Cycle #150+):** A process can be alive (`ps aux` confirms) yet completely hung — producing zero output files for 80+ minutes. The workspace contains only the original script file. This is NOT the same as a healthy long API call (which produces output files as it progresses). **Diagnostic**: after confirming process alive via Method 1, check workspace file count. If only the script exists and nothing else after 60+ minutes, the process is hung. Block it.

**Results-complete reclaim workflow (added Cycle #225, extended 2026-06-01):** When a task has results.json + output.log with completion markers but is still `running`, reclaim then IMMEDIATELY block. See `references/reclaim-then-block-for-results-complete.md` and `references/director-reclaim-decision-flow.md`. The synthesis worker can read the workspace files directly — the experiment data doesn't need the kanban task to be "done" for synthesis to work. **Contrast with "alive but silent"**: The silent pattern has NO output files. This pattern has COMPLETE output files but no completion signal. Both require reclaim, but the "results complete" variant means the data is recoverable without re-running the experiment.

**Pitfall — "Stale files from previous run" (added Cycle #163):** A workspace may contain output files that are OLDER than the current process start time — they're remnants of a prior attempt, not evidence the current run is progressing. In Cycle #163, exp_367 had a `progress.json` last modified at 11:10 but the current process started at 11:20 — the file was from a previous stuck run. **Diagnostic**: when checking workspace files (Method 3/4), compare file mtimes against the process start time from `ps aux`. If ALL output files predate the process start, the files are from a previous run and the current run is producing nothing. Treat this the same as "zero output files" — the current process is hung. **Fix**: check `ps aux` for the process start time (`lstart` column or the time column), then compare against the latest file mtime in the workspace. If `mtime < process_start`, the files are stale artifacts, not current output.

**Pitfall — "Just dispatched" false positive (added Cycle #151):** Tasks that were dispatched in the current cycle show 0 files and 999m age in the workspace staleness check. This is NORMAL — the worker process hasn't started writing files yet. Do NOT diagnose as stuck. The correct check: (1) confirm the process exists via `ps aux`, (2) wait 5+ minutes, (3) re-check workspace. If the process exists and the task is <10 minutes old, leave it alone regardless of workspace state.

**Pitfall — False positive error detection from output log keyword scanning (added Cycle #175):** When scanning output logs for health status, naive keyword matching on "error" produces false positives. Experiment output logs frequently contain the word "error" in analytical context — e.g., "when facts cause errors on Q5", "error analysis shows 3 patterns", "[FAIL] Connection reset by peer" (a transient retry, not a crash). In Cycle #175, 6/6 tasks with output logs were initially flagged as "ERROR" because every log contained the word "error" in error-analysis prose, not actual failures. **Fix**: Check for ACTUAL failure signals: `Traceback`, `exit code`, `FAILED` (capitalized), or `killed`. Do NOT flag based on the bare word "error" appearing in log content. Better yet, check the last 3 lines of the log for completion markers (`accuracy:`, `score:`, `results:`) vs crash markers (`Traceback`, `Error:` at line start).

**Blind spot — output logs outside workspace (added Cycle #151):** Many experiment scripts write output to `~/.hermes/experiments/` or `/tmp/` rather than the workspace directory. The workspace staleness check (Method 3) will show these tasks as "STALE" or "DEAD" even when they're actively writing to external log files. When a task shows STALE but `ps aux` confirms the process is alive, check for output logs: `ls -lt ~/.hermes/experiments/exp_*_output.log 2>/dev/null | head -5`. The most recent log file's modification time is the true last-activity signal.

**Pitfall — `find` command workspace matching too broad for experiment lookup (added Cycle #184).** When searching for workspaces containing specific experiment files, using `find ~/.hermes/kanban/workspaces -maxdepth 1 -type d -name "t_*"` combined with `ls "$ws"/*${exp}*` matches ALL workspaces because experiment IDs like "exp_466" appear as substrings in filenames within many workspaces (e.g., a script that references exp_466 in a comment or variable). In Cycle #184, this pattern returned all 43 workspaces for each of the 5 target experiments — useless for locating the specific workspace. **Fix**: Use the done task list to find task IDs first, then look up workspaces by task ID:
```python
# CORRECT — find task ID from done list, then look up workspace
done = json.load(open('/tmp/kanban_done.json'))
for t in done:
    if 'exp_466' in t.get('title', ''):
        task_id = t['id']
        workspace = f'~/.hermes/kanban/workspaces/{task_id}/'
        break
```
The workspace path is always `~/.hermes/kanban/workspaces/<task_id>/` — use the task ID from the kanban list, not filesystem glob patterns.

**Reusable scripts:** `scripts/director_health_check.py` runs Method 3 in bulk. `references/director-desync-detection.py` detects status desync. `references/reclaim-then-block-for-results-complete.md` covers the results-complete reclaim workflow. `references/director-reclaim-decision-flow.md` has the decision tree. `references/director-pass-cycle245-example.md` is a worked example of a full Director pass (status desync detection, queue classification, task creation decisions).

**When to use each method:**
- **Method 1 (`ps aux`)**: Ground truth for process liveness. Use first.
- **Method 2 (heartbeat events)**: Shows detailed progress and timing. Use when you need to know *what* the worker is doing, not just *if* it's alive. Requires `kanban show --json` per task (slow for 10+ tasks).
- **Method 3 (workspace mtime)**: Fastest bulk check — single filesystem scan covers all tasks. Use for quick triage when you have 10+ running tasks and want to identify which ones need deeper inspection.
- **Method 4 (workspace file count)**: After Method 1 confirms process alive, check if workspace has output files beyond the original script. Zero output files after 60+ minutes = hung process, not long computation. This catches the "alive but silent" pattern that Methods 1-3 miss.

**Batch liveness check (for 10+ running tasks):**
```bash
# Dump all running tasks
hermes kanban list --status running --json 2>&1 > /tmp/kanban_running.json

# Check heartbeats in batch (separate command)
python3 << 'EOF'
import json, datetime
d = json.load(open('/tmp/kanban_running.json'))
now = datetime.datetime.now(datetime.timezone.utc).timestamp()
for task in d:
    tid = task['id']
    title = task.get('title', 'N/A')[:40]
    # Note: list output doesn't have events — need individual show for heartbeat check
    print(f"{tid}: {title} (use kanban_show for heartbeat detail)")
EOF
```

**Limitation:** `kanban list --json` does NOT include the `events` array — you must use `kanban show <id> --json` for each task individually. For 10+ tasks, batch the file dumps first (`for tid in ...; do hermes kanban show $tid --json > /tmp/task_${tid}.json; done`), then process all files in one Python command.

**Monitoring-script false positive from events omission (added Cycle #145, exp_203).** When a cron job or monitoring script reads `kanban list --json` and checks for events (heartbeats, completions), it sees 0 events for ALL tasks — not because they're stuck, but because the list endpoint simply doesn't return events. In exp_203, this caused 3 tasks to be incorrectly blocked as "stuck" when they were actually healthy workers with active Python processes. **Diagnostic**: If a monitoring script reports "0 events" for multiple tasks simultaneously, suspect the list-endpoint omission, not mass worker failure. Always verify with `kanban show <id> --json` (which DOES return events) or `ps aux | grep hermes` before blocking. **Rule**: Never use `kanban list --json` events as a liveness signal — it's always 0.

