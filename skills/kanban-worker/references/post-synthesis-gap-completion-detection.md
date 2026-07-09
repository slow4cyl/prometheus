# Post-Synthesis Gap Completion Detection (added Cycle #271)

## Problem

When the Director dumps the done list, creates a synthesis task, and dispatches it, experiments can complete in the gap between the dump and the synthesis worker starting. The synthesis worker reads the comment thread when it starts, but if no comment was added for the gap completion, the experiment is missed.

## Example (Cycle #271)

1. Director dumps done list → exp_955, exp_997 identified as unsynthesized
2. Director creates synthesis task t_7d741309 for exp_955 + exp_997
3. exp_1002 completes (t_dafb4115 moves to done)
4. Director re-checks done list → discovers exp_1002 is also unsynthesized
5. Director adds exp_1002 to synthesis task via `kanban_comment`

Without step 4-5, exp_1002 would have been missed by synthesis.

## Detection Pattern

```bash
# Step 1: Initial dump (before creating synthesis task)
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done.json

# Step 2: Create synthesis task
hermes kanban create "SYNTHESIS: ..." --assignee prometheus-synthesis --body "..."

# Step 3: Re-dump done list (after creating synthesis task)
hermes kanban list --status done --json 2>&1 > /tmp/kanban_done_recheck.json

# Step 4: Diff in a separate command (TIRITH blocks pipes)
python3 << 'EOF'
import json, re
original = json.load(open('/tmp/kanban_done.json'))
recheck = json.load(open('/tmp/kanban_done_recheck.json'))
original_ids = {t['id'] for t in original}
new_completions = [t for t in recheck if t['id'] not in original_ids]
for t in new_completions:
    title = t.get('title', '')
    if 'synth' not in title.lower() and 'consolidat' not in title.lower():
        for m in re.finditer(r'exp_(\d+\w*)', title):
            print(f"GAP COMPLETION: exp_{m.group(1)} in {t['id']}: {title[:80]}")
EOF

# Step 5: Add gap completions to synthesis task
hermes kanban comment <synth_task_id> "ADDITIONAL: Also synthesize exp_XXX: <summary>"
```

## Why This Matters

The "Late-completing experiments" pitfall (documented elsewhere) describes the reaction (add via comment). This document describes the **proactive detection** step that prevents the race condition from occurring in the first place.

The window is narrow (typically 1-5 minutes between dump and synthesis worker start), but experiments that complete in this window are the most common source of missed synthesis entries.
