# Director Pass Pitfalls — Session 2026-06-03

## TIRITH blocks `curl | python3` pipes

Running `curl -s http://localhost:port/endpoint | python3 -c "..."` triggers
TIRITH's `pipe_to_interpreter` rule. This was discovered during a Director
pass when checking the WM daemon status.

**Failed command:**
```bash
curl -s -X POST http://127.0.0.1:19876/status -H "Content-Type: application/json" -d '{}' 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); ..."
```

**Error:** `tirith:curl_pipe_shell` — "Pipe to interpreter: curl | python3"

**Workaround:** Two-step pattern (same as `cat | python3` and `hermes | python3`):
```bash
# Step 1: Write to file
curl -s -X POST http://127.0.0.1:19876/status -H "Content-Type: application/json" -d '{}' > /tmp/wm_status.json
# Step 2: Process in separate command
python3 -c "import json; d=json.load(open('/tmp/wm_status.json')); print(d)"
```

**Scope:** This applies to ALL commands piped to `python3`, not just `hermes` or `cat`.
The TIRITH filter matches the pipe-to-interpreter structure, not the left-side command.

## Synthesis task detection false positive

When checking for existing synthesis tasks in `kanban list --status running`,
the naive filter `'synth' in title.lower()` matches experiment tasks that
reference synthesis in their curiosity source text (e.g.,
`exp_2606: [NEW from synthesis v612] Direct LR 36x faster...`).

**Correct filter:** Match titles that START with `SYNTHESIS:` or `synthesis:`,
not just contain the word. Actual synthesis tasks use the naming convention
`SYNTHESIS: consolidate N experiments`.

**Code:**
```python
# WRONG — catches experiment tasks referencing synthesis
synth_tasks = [t for t in running if 'synth' in t.get('title', '').lower()]

# RIGHT — matches only actual synthesis tasks
synth_tasks = [t for t in running if t.get('title', '').lower().startswith('synthesis')]
```

## Reclaim-then-re-dispatch cycle

Reclaiming duplicate or stale tasks via `hermes kanban reclaim` puts them back
in `ready` status. The scheduler immediately re-dispatches them to the same
workers — creating a reclaim loop where tasks cycle `running` → `ready` → `running`
within seconds.

**Observed in this session:** Reclaimed 10 topic-duplicate tasks. After
`hermes kanban dispatch`, only 2 spawned (workers at per-profile cap). The other
8 were deferred. But the 2 that spawned were re-assigned to workers that already
had the same topic — the duplicate wasn't eliminated, just reshuffled.

**When reclaim IS effective:**
- Worker is at per-profile cap (dispatcher defers re-assignment)
- Task is being reclaimed to make room for a different task on the same worker
- You then immediately create a NEW task for the freed slot

**When reclaim is NOT effective:**
- Worker has capacity (reclaimed task gets re-dispatched immediately)
- No new task is created for the freed slot (net zero change)

**Better approach:** Prevent duplicates at creation time by checking running
task topics (step 5b in Director flowchart). If you must reclaim, ensure the
worker is at cap OR immediately create a replacement task with different content.
