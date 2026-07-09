# Director Quick-Pass Flowchart — Variant 6 Synthesis Check

## New Step 1b: Verify Synthesis Tasks in Running List

After dumping board state (step 1), check for synthesis tasks that may have completed but still appear in the running list (Variant 6 desync):

```bash
# Extract synthesis tasks from running list
python3 -c "
import json
running = json.load(open('/tmp/kanban_running.json'))
for t in running:
    title = t.get('title', '')
    if 'synth' in title.lower() or 'consolidat' in title.lower():
        print(f'{t[\"id\"]}: {title[:60]}')
"
```

For each synthesis task found:
```bash
hermes kanban show <synthesis_task_id> --json 2>&1 > /tmp/synth_check.json
python3 -c "import json; d=json.load(open('/tmp/synth_check.json')); print(d.get('task',{}).get('status','?'))"
```

If status=done:
- The synthesis is already complete — do NOT create a replacement
- Exclude from saturation count (it's not consuming a worker)
- Note its experiment IDs so you don't create synthesis for those experiments again

If status=running:
- It's genuinely running — include in saturation count
- If it covers experiments you need synthesized, use `kanban_comment` to expand scope (don't create duplicate)

## Why This Matters

Synthesis tasks complete fast (<2 min). The kanban list endpoint can show them as "running" for minutes after completion. Without this check, the Director creates a duplicate synthesis task that wastes a worker slot and risks concurrent writes to self_state.json.

This is the pass-spanning variant of the "Duplicate synthesis task in same Director pass" pitfall (Cycle #161). That pitfall covers creating two synthesis tasks in one pass; this covers a synthesis task from a *previous* pass that desyncs into the current pass's running list.

## Real-World Example (Director #246)

- synthesis t_f7e25100 (exp_1355) completed successfully
- Next Director pass found it in running list (desync)
- Without the check, would have created a second synthesis for exp_1355
- With the check: verified status=done, skipped, created synthesis only for unsynthesized exp_1349 + exp_1354
