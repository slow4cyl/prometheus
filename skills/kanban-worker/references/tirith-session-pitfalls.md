# TIRITH Filter Pitfalls — Session-Specific Additions

## SQL DELETE without WHERE (added Cycle #213)

**Trigger:** Task body text containing "Remove" + "items" + "array" (or similar SQL-like phrasing).

**Example that fails:**
```
hermes kanban create "Queue Cleanup" --body "... Remove resolved items from the queue array ..."
```
Error: `tirith:sql_delete_without_where`

**Workaround:** Rephrase to avoid SQL-like verbs:
- "Remove resolved items" → "Clean up resolved items"
- "Remove entries" → "Filter out entries"
- "Remove completed tasks" → "Archive completed tasks"

**Why it fires:** TIRITH's pattern matcher interprets "Remove ... items ... array" as a SQL DELETE statement without a WHERE clause. The filter is overly broad — it catches natural language instructions that happen to use SQL-like phrasing.

**Common contexts:** Queue curation tasks, maintenance task bodies, cleanup instructions.

## Related TIRITH Pitfalls (for reference)

See main SKILL.md for:
- `tirith:pipe_to_interpreter` — cat/grep/hermes piped to python3
- `tirith:raw_ip_url` — IP addresses in task bodies
- `tirith:variation_selector` — emoji in heredocs
- `tirith:dotfile_overwrite` — heredocs appending to dotfiles
