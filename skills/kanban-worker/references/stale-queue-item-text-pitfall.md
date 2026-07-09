# Stale Queue Item Text Pitfall (Cycle #223)

## Problem

Queue items can contain stale claims about experiment status. Most commonly:
- `[UNTESTED] exp_XXX: ... This experiment was never run.`
- `[NEW from exp_XXX] ...` where exp_XXX has since completed

The text was generated at synthesis time and never updated when the experiment later completed.

## Real Example (Cycle #223)

Queue item 62:
```
[UNTESTED] exp_879: Coherence scrutiny -- chain-of-thought dependent? This experiment was never run.
```

But exp_879 WAS completed (task t_68b8524f) and consolidated by synthesis v383. The synthesis summary confirmed: "Coherence scrutiny CoT-dependent — 1.75x amplification with CoT, 1.0x without (exp_879 CONFIRMED)."

## Impact

Creating a new task for a "never run" experiment that's actually done wastes a worker on duplicate work. In this case, exp_915 was created to test the same question exp_879 already answered.

## Detection

Before creating tasks for UNTESTED/never-run queue items:

1. Extract experiment IDs from the item text via regex: `re.findall(r'exp_(\d+)', text)`
2. Check each ID against `self_state.json`'s `experiments.completed` array
3. Check against the done task list (`kanban list --status done --json`)
4. If the experiment IS completed, the item is stale — tag RESOLVED instead of creating a task

## Why the Classification Misses This

The standard queue classification (NO_SOURCE/RUNNING/DONE/UNKNOWN) checks whether the source experiment is in `ss_exp_ids`. But UNTESTED items without explicit `[NEW from exp_XXX]` prefixes have no source ID to classify — they appear as NO_SOURCE and bypass the cross-reference check.

**Fix**: After classification, specifically check NO_SOURCE items for embedded experiment IDs (even without the `[NEW from exp_XXX]` prefix) and cross-reference against completed experiments.

## Prevention

Include this check in the Director's queue triage step:
```python
# After standard classification, check UNTESTED items for stale claims
for i, item in enumerate(queue):
    text = item.get('text', str(item)) if isinstance(item, dict) else str(item)
    if 'UNTESTED' in text or 'never run' in text.lower():
        source_ids = re.findall(r'exp_(\d+)', text)
        for sid in source_ids:
            eid = 'exp_%s' % sid
            if eid in ss_exp_ids:
                print("STALE: [%d] claims %s never run, but it's completed" % (i, eid))
                # Tag as RESOLVED, do not create task
```
