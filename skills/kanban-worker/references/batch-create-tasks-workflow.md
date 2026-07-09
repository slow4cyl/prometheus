# Batch Task Creation Workflow (Director)

## `batch_create_tasks.py` — Preferred Approach

When the script exists at `~/.hermes/scripts/batch_create_tasks.py`, use it instead of manual task creation. It handles scoring, diversity capping, and assignment in one pass.

### CLI Interface

```
usage: batch_create_tasks.py [-h] [--count COUNT] [--min-score MIN_SCORE]
                             [--dry-run] [--json]

options:
  -h, --help            show this help message and exit.
  --count COUNT         Max tasks to create (default: 15)
  --min-score MIN_SCORE Minimum score threshold (default: 60)
  --dry-run             Show what would be created
  --json                JSON output
```

### Parameters

- `--count N`: Max tasks to create. Match to free worker count.
- `--min-score N`: Minimum priority score. Default 60 is conservative; use 30 for broader coverage when free workers are plentiful.
- `--dry-run`: Preview without creating. Always run first.
- `--json`: Machine-readable output for programmatic use.

### Typical Director Pass

```bash
# Step 1: Dry run to see what would be created
python3 ~/.hermes/scripts/batch_create_tasks.py --count 9 --min-score 30 --dry-run

# Step 2: Create for real
python3 ~/.hermes/scripts/batch_create_tasks.py --count 9 --min-score 30

# Step 3: Dispatch (spillover is normal — tasks may not spawn immediately)
hermes kanban dispatch
```

### Output Interpretation

The script prints:
- `Queue: N active items, M score >= threshold`
- `Workers: N free, M busy`
- `Uncovered by running tasks: N`
- `Selected: N tasks (diversity cap: M/thread)`
- Thread distribution and created task IDs

### When to Use Manual Creation Instead

1. Script is too conservative — creates fewer tasks than 50% of free worker slots (includes diversity cap under-creation, not just min-score)
2. Coverage check reports "uncovered: 0" but free_workers > 3 (Jaccard threshold too aggressive — see batch-creator-coverage-pitfall.md)
3. Tasks need custom bodies not derivable from queue items
4. Script doesn't exist or errors out
5. **ALL selected candidates are topic-covered by running experiments** — the script's source-ID check misses NO_SOURCE items whose topic is already in flight. See `references/batch-creator-selects-covered-items.md` for detection code. When this happens, create ZERO tasks (convergence, not conservatism).

### Conservative Scoring Pitfall

The script can be overly conservative. In Cycle #248 it created only 1 task when 12 workers were free (min-score=60). Fix: lower `--min-score` to 30. See `batch-create-conservative-pitfall` in director-loop skill references.

### Diversity Cap Under-Creation (added 2026-06-02)

The script applies a 35% per-thread diversity cap (floor(0.35 × N) per thread). With few active threads, this severely limits task count. In Director Pass 589 (2026-06-02), 4 free workers existed but the script only created 2 tasks because the diversity cap limited it to 1/thread across 2 active threads.

**Detection:** If `dry-run` output shows `Selected: N tasks` where N < 50% of free workers, the diversity cap is the bottleneck.

**Fix:** After batch creation, manually create additional tasks for remaining free workers using the NO_SOURCE queue items with lowest coverage. The batch script handles the hard part (scoring, dedup); manual creation fills the gap.

**When to skip the batch script entirely:** When free workers ≥ 5 and the script creates fewer than 3 tasks, it's faster to manually create all tasks than to run the script + supplement.

### Duplicate/Wrong Experiment IDs (added Cycle #249)

The script can assign incorrect experiment IDs — e.g., exp_1, exp_2, exp_3 instead of the correct sequential IDs from prometheus.db. In Cycle #249, 7 tasks were created with IDs exp_1 through exp_4, while the actual counter was at 1807+. This produces duplicate IDs across tasks (two tasks claiming exp_1, two claiming exp_2, etc.).

**Detection:** After batch creation, verify IDs:
```bash
hermes kanban list --status ready --json 2>&1 > /tmp/kanban_ready.json
python3 -c "
import json, re
tasks = json.load(open('/tmp/kanban_ready.json'))
for t in tasks:
    m = re.search(r'exp_(\d+)', t.get('title',''))
    if m and int(m.group(1)) < 100:
        print(f'WRONG ID: {t[\"id\"]}: {t[\"title\"][:80]}')
"
```

**Fix:** If wrong IDs detected, manually reassign via kanban_comment before dispatch:
```bash
hermes kanban comment <task_id> "NOTE: Correct experiment ID is exp_NNN (not exp_M). Worker should use exp_NNN for results."
```

**Impact:** Workers use the ID from their task title for self_state.json writes. Duplicate IDs cause synthesis to overwrite earlier experiment entries. The Director should check for duplicates at the start of every pass (see duplicate detection code in kanban-worker skill).

### `--json` Path Crashes with UnboundLocalError (added 2026-06-02, updated 2026-06-02)

When running with `--json`, the script crashes with `UnboundLocalError`. Two bugs were found:

1. **`next_id` undefined (original bug):** The `next_id` variable is defined on line 326 (in the task creation path) but referenced on line 287 (in the JSON output path) — before it's initialized.
   - **Fix:** Move `next_id = [NEXT_EXP_ID]` before the `if args.json:` branch (~line 279).

2. **`i` undefined (incomplete fix):** After fixing `next_id`, the JSON output path still crashes with `UnboundLocalError: cannot access local variable 'i'`. The `i` variable is used in title generation (`f"exp_{next_id[0] + i}: {s['text'][:80]}"`) but is only defined in the creation loop.
   - **Fix:** Use enumerate or a counter in the JSON output path: `for i, s in enumerate(selected):`.

**Workaround:** Avoid `--json` flag. Use `--dry-run` for preview (human-readable output) and process the text output manually. The `--json` flag is broken as of 2026-06-02.

### JSON Output Field Name

The `--json` output uses `total` (not `score`) for the priority score. Filtering on `item.get('score', 0)` returns 0 results. Always use `item.get('total', 0)`. See `curiosity-scorer-json-format` in director-loop skill.

### Dry-Run vs Actual Diversity Cap Discrepancy (added Cycle #245)

The dry-run output can show a MORE GENEROUS diversity cap than actual execution. In Cycle #245, `--dry-run` showed `diversity cap: 3/thread` and selected 4 tasks, but actual execution used `diversity cap: 1/thread` and only created 2. This causes the Director to think 2 tasks need manual creation — but one of those may already exist from the batch creator's partial run.

**Root cause:** The dry-run and actual execution can use different cap calculations (e.g., dry-run counts free workers differently, or the cap formula changed between code paths).

**Fix:** After batch creation, ALWAYS verify what was actually created before supplementing manually:

```bash
# Step 1: Run batch creator
python3 ~/.hermes/scripts/batch_create_tasks.py --count N --min-score 30

# Step 2: IMMEDIATELY check what it created (don't skip this)
hermes kanban list --status ready --json 2>/dev/null > /tmp/kanban_ready.json
python3 -c "
import json, re
tasks = json.load(open('/tmp/kanban_ready.json'))
for t in tasks:
    m = re.search(r'exp_(\d+)', t.get('title',''))
    if m: print(f'{t[\"id\"]}: exp_{m.group(1)} — {t[\"title\"][:60]}')
"

# Step 3: ONLY THEN create manual tasks for remaining free workers
# Check each manual task topic against the batch-created tasks to avoid duplicates
```

**Detection:** If dry-run says "Selected: N" but actual creation says "Created: M" where M < N, the cap discrepancy is the cause. Check the actual created list before supplementing.

**Prevention:** Treat the batch creator output as authoritative. The dry-run is a preview, not a guarantee. Always verify actual creation results before manual supplementation.
