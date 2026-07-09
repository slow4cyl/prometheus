# Batch Creator Incorrect Experiment ID Pitfall (Cycle #249)

## Problem

`batch_create_tasks.py` can create tasks with experiment IDs starting from `exp_1` instead of reading the next sequential ID from `prometheus.db`. This happens when the script doesn't query the database counter before generating task titles.

## Example

In Cycle #249, the batch creator produced:
- `exp_1: [NEW from synthesis v563] 5-sample transfer learning floor...`
- `exp_2: [NEW from synthesis v413] Response consistency detection...`
- `exp_3: [NEW from synthesis v451] Direct LR matrix multiply...`
- `exp_4: [NEW from synthesis v563] Cross-domain feature transfer...`

The DB counter was at 1804. These should have been exp_1806–exp_1809.

## Impact

- Tasks run to completion with wrong experiment IDs
- Synthesis worker adds wrong IDs to self_state.json
- Experiment tracking becomes inconsistent
- Manual cleanup required to reconcile IDs

## Detection

After batch creation, check task titles for IDs lower than the current DB counter:
```bash
python3 -c "
import sqlite3, re
c = sqlite3.connect('~/.hermes/prometheus.db')
r = c.execute('SELECT id FROM experiments').fetchall()
ids = [int(re.search(r'(\d+)', x[0]).group(1)) for x in r if re.search(r'(\d+)', x[0])]
print(f'Next valid ID: {max(ids)+1 if ids else 1}')
"
```

## Fix (Manual Task Creation)

When creating tasks manually (NOT via batch_create_tasks.py), always query the DB first to get the next sequential ID. Title tasks as `exp_NNN: <hypothesis>` where NNN is the next valid ID.

## Prevention

The `batch_create_tasks.py` script should read the DB counter before generating titles. Until fixed, Director should:
1. Run batch creator with `--dry-run` first
2. Verify the generated experiment IDs are sequential and above the DB counter
3. If incorrect IDs are found, reclaim those tasks and recreate manually with correct IDs

## Related

- `references/batch-create-tasks-workflow.md` — full batch creator workflow
- `references/batch-creator-coverage-pitfall.md` — Jaccard threshold too aggressive
