---
name: prometheus-dashboard
description: Fix, improve, and extend the Prometheus dashboard (prometheus_dashboard_v2.py) — the SSR Python HTTP server at port 8889. Covers chart/visualization debugging, data-shape analysis before rendering, CSS+Python co-fix patterns, and the restart procedure.
---

# Prometheus Dashboard

## When to Use

- A chart, panel, or tab on the dashboard at `http://localhost:8889/` looks broken, unreadable, or unruly
- Adding or modifying a visualization (bar chart, lineage chain, transfer flow, timeline)
- Debugging why a panel renders incorrectly (overflow, clipping, invisible bars, label overlap)
- Any work on `~/prometheus_dashboard_v2.py`

## Architecture

`prometheus_dashboard_v2.py` is a single-file Python HTTP server (~2300 lines) using `http.server.ThreadingHTTPServer`. No external dependencies — pure stdlib.

- **Port**: 8889
- **Rendering**: SSR (server-side rendering). Python functions generate HTML strings with inline CSS. No JS framework — vanilla JS only for tab switching and AJAX refresh.
- **CSS**: Embedded in a single `CSS` string constant (starts ~line 1040). Dark theme with CSS variables.
- **Data**: SQLite at `~/.hermes/prometheus.db`. Each panel has a `get_*()` data function and a `render_*()` HTML function.
- **Caching**: `_render_cache` (5s TTL) for API responses, `_exps_cache` (30s TTL) for expensive experiment loads.
- **Tabs**: Overview, Live Activity, Cron Monitor, Timeline, Lineage, Domains, RAG Quality, Health

Key render functions:
- `render_lineage(lin)` — depth distribution chart, sample chain, transfer flows
- `render_domains(domains)` — domain mastery tiers
- `render_health(health)` — alerts, resources, throughput, claim lifecycle
- Timeline uses on-demand AJAX (`/api/phases` + `/api/phase/N/experiments`)

## User Preferences for This Task Class

- **Investigate before changes.** The user says "don't make changes yet" — do a full diagnosis of what's wrong and present findings first. Present root cause, data analysis, and proposed fix. Wait for approval.
- **Scoped incremental fixes.** When multiple issues exist, the user picks which to fix first ("just the depth chart first"). Don't fix everything at once — fix one thing, verify it, then move to the next.
- **Direct communication.** No hand-holding. Present the diagnosis concisely, make the fix, confirm it works.

## Debugging Pattern for Chart/Visualization Issues

1. **Read the render function and CSS** for the broken panel. Use `read_file` with offset to find the relevant `render_*()` function and its CSS classes.
2. **Pull the actual data** from the SQLite DB to understand the data shape. Reproduce the `get_*()` query in `execute_code` or `terminal` to see what the function is working with.
3. **Diagnose the mismatch** between data shape and CSS layout:
   - Too many items for the container width? (overflow, clipping)
   - One dominant value making everything else invisible? (linear scale problem)
   - Labels overlapping? (too many labels in too little space)
   - Text overflowing containers? (no word-break, no truncation)
   - Data semantics wrong? (e.g., self-loops in "cross-domain" flows)
4. **Present findings** with concrete numbers before fixing.
5. **Fix CSS and Python together** — the rendering logic and styling are co-dependent. Patch both in the same pass.
6. **Restart the server** and verify the rendered HTML.

## Restart Procedure

The dashboard runs as a background process. To restart after edits:

```
# Find the PID
ps aux | grep prometheus_dashboard_v2 | grep -v grep

# Kill old, start new
kill <PID>
# Then use terminal(background=true) to start:
python3 ~/prometheus_dashboard_v2.py
```

Verify after restart:
```
curl -s http://127.0.0.1:8889/api/lineage | python3 -c "import sys,json; print(len(json.load(sys.stdin)['active_depths']))"
```

Or fetch the rendered HTML and check the specific section:
```
curl -s http://127.0.0.1:8889/ | python3 -c "
import sys, re
html = sys.stdin.read()
# Extract section by ID
m = re.search(r'id=\"section-LINEAGE\"(.*?)(?:<div class=\"section\"|$)', html, re.DOTALL)
if m: print(m.group(1)[:500])
"
```

## Common Visualization Pitfalls

### Too many bars/items for container width
Symptom: bars overflow or clip (`overflow:hidden` on parent swallows them). Fix: bucket data into ranges, or add `overflow-x:auto` to the container.

### Linear scale dominated by one outlier
Symptom: one bar is full height, everything else is invisible nubs. Fix: use `math.sqrt()` or `math.log()` scale for bar heights. Sqrt is better for count data (preserves zero, gentle compression).

### Label overlap
Symptom: depth/axis labels pile on top of each other. Fix: reduce label count (bucket, skip every Nth), use `white-space:nowrap` to prevent wrapping, or rotate labels.

### Panel clips content
`.panel` has `overflow:hidden` which clips overflowing bars. Either fix the bar count/sizing or add `overflow-x:auto` to the chart container specifically.

### Text walls in chain/list views
Symptom: long text items with no truncation create an unreadable wall. Fix: add `max-height` + `overflow-y:auto` for scroll, truncate long text with CSS `text-overflow:ellipsis`, or collapse items with expand-on-click.

### Query returns wrong data subset
Symptom: "cross-domain transfers" includes self-loops (source==target). Fix: add `WHERE source_domain != target_domain` to the SQL query. Always verify the data semantics match the panel title.

## Domain Tiers Panel — Data Source

The "Domains" tab with mastery/emerging/long_tail tiers reads from the `domains` table in `prometheus.db`, NOT from `experiments` directly. The `get_domains()` function (line ~416) joins `domains.name` to `experiments.domain` to count experiments per domain, but the domain list itself comes from the `domains` registry table.

If a domain shows in the dashboard but shouldn't (e.g., "unknown" with 1 experiment in the long tail), or if emerging domains are missing, check the `domains` table:

```sql
-- Is the junk domain still in the registry?
SELECT name, confidence FROM domains WHERE name = 'unknown';
-- Are emerging domains missing from the registry?
SELECT e.domain, COUNT(*) FROM experiments e
LEFT JOIN domains d ON d.name = e.domain
WHERE d.name IS NULL AND e.domain IS NOT NULL AND e.domain != ''
GROUP BY e.domain ORDER BY COUNT(*) DESC;
```

The `domains` table is auto-rebuilt by `normalize_all_domains.py` (called by the `domain_taxonomy_maintenance` cron every 15m). If that cron step is crashing, the registry stays stale. See the `prometheus-domain-investigation` skill for details on that crash and how to fix it.

Also note: `build_topology_export.py` builds `topology_full_export.json` which has its own domain data (from `experiments`, not from the `domains` table). The dashboard may read from both sources depending on the panel.

- `~/prometheus_dashboard_v2.py` — the entire dashboard (single file)
- `~/.hermes/prometheus.db` — SQLite database (experiments, curiosities, domains, worker_results, transfer_tracking, etc.)
- `~/.hermes/cron/jobs.json` — cron job definitions (read by dashboard)
- `~/.hermes/topology_full_export.json` — topology export (read by dashboard)

## References

- `references/lineage-chart-fix.md` — detailed writeup of the depth chart bucketing + sqrt scale fix (2026-06-30 session)
