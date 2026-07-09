# Reference Files Index

## 🔴 DIAGNOSTIC ENTRY POINT — Dashboard / Pipeline Health

When the dashboard shows stale experiment counts or kanban done count
diverges from dashboard count, start here:

- `references/dashboard-experiment-count-lag.md` — **Full diagnostic guide.**
  Contains: pipeline architecture, 5 root causes, diagnosis commands, scripts
  reference, file locations, systemd services, and lessons learned. Read this
  FIRST when investigating dashboard staleness.

Quick health check:
```sql
-- In prometheus.db: are worker_results being applied?
SELECT applied, COUNT(*) FROM worker_results GROUP BY applied;

-- In kanban.db: how many done tasks lack results?
SELECT CASE WHEN result IS NOT NULL AND result != '' THEN 'has' ELSE 'no' END, COUNT(*)
FROM tasks WHERE status='done' GROUP BY 1;
```

The structural gate (`_enforce_worker_result_written` in `tools/kanban_tools.py`)
blocks `kanban_complete` on `exp_*` tasks without a `worker_results` row.
Implemented June 4 2026. See "PITFALL: Free-Tier Models Drop Result Writing"
in the main SKILL.md.

---

## Synthesis & Director References

- `references/synthesis-title-parsing-pitfall.md` — Comma-separated experiment ID format in synthesis titles. Two-step parser to extract all IDs.
- `references/director-workspace-interpretation.md` — How to interpret workspace staleness states during Director passes. Includes state table, pitfalls, and Cycle #219 example.

## Existing References (from prior cycles)

- `references/director-queue-triage-framework.md` — Queue item classification taxonomy
- `references/director-pass-template.md` — Structured Director pass audit format
- `references/director-worker-availability.md` — Why `kanban list` can't determine worker occupancy
- `references/director-stuck-task-protocol.md` — Decision flowchart for stuck workers
- `references/director-desync-detection.py` — Status desync detection script
- `references/dashboard-visibility-troubleshooting.md` — Dashboard debugging
- `references/status-desync-variants.md` — Taxonomy of list/show/ps desync patterns
