# Pipeline Pitfalls — June 2026 Migration

## Queue corruption from synthesis workers

Synthesis workers writing to self_state.json can corrupt the curiosity queue
by serializing Python dicts instead of plain strings. Produces nested JSON
escaping like `{'text': '{\\'text\\': ...'}`.

**Detection:** Queue size jumps to thousands. Items show `{'text':` prefix.
**Fix:** Run curiosity_scorer.py which filters corrupted items. Director
detected this autonomously and spawned repair tasks.

## sync_sqlite_to_state.py overwrites queue repairs

The dashboard watchdog runs sync_sqlite_to_state.py every 2 minutes. It used
to read curiosities from SQLite and overwrite self_state.json's queue,
destroying repairs.

**Fix:** Removed queue overwrite from sync_sqlite_to_state.py. self_state.json
is the authoritative source for the queue. SQLite curiosities is just a
tracking log. If queue count jumps back to thousands after repair, check
if this script was reverted.

## Experiment IDs: exp_AUTO → sequential

batch_create_tasks.py used `exp_AUTO:` prefix because it didn't track IDs.
Now reads highest ID from SQLite and increments (exp_1673, exp_1674, ...).

## Timezone config for kanban timestamps

Empty `timezone: ''` in config.yaml causes UTC display (4-5h behind CDT).
Fix: `hermes config set timezone America/Chicago` + `sudo timedatectl set-ntp true`.

## Direct kanban → SQLite sync

sync_kanban_to_db.py bypasses synthesis bottleneck. Reads completed kanban
tasks, extracts experiment IDs and run summaries, writes directly to
prometheus.db. Run when synthesis falls behind.

## Curiosity scorer deduplication

is_already_answered() compares queue items against completed experiment
hypotheses (overlap > 0.4 = already answered). batch_create_tasks.py also
checks completed experiments AND results before creating tasks. Prevents
Director from re-investigating already-answered questions.
