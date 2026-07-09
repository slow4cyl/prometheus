# Dashboard Visibility Troubleshooting

When someone reports "nothing shows up on kanban," two separate dashboard systems may be involved. Confusing which one they're looking at is the #1 source of wasted debugging time.

## Two Dashboard Systems

| System | Port | What it shows | URL |
|---|---|---|---|
| **Prometheus custom dashboard** | 8888 | Experiments feed, curiosity queue, skills, knowledge graph radar, cost doughnut, tag distribution | `http://127.0.0.1:8888/kanban` |
| **Hermes built-in dashboard** | 9119 | Kanban board with columns (Triage → Todo → Scheduled → Ready → In Progress → Done), task cards, worker lanes | `http://127.0.0.1:9119/kanban` |

**Key distinction:** The Prometheus dashboard at 8888 does NOT show the kanban board columns — it shows an experiment-centric view (feed, queue, skills). If someone says "kanban" they likely mean the Hermes dashboard at 9119.

## Debugging Checklist

### 1. Verify tasks exist in the DB
```sql
-- kanban.db (Hermes kanban system)
SELECT status, COUNT(*) FROM kanban.db tasks GROUP BY status;

-- prometheus.db (Prometheus state)
SELECT key, value FROM system WHERE key IN ('version', 'last_updated');
SELECT COUNT(*) FROM heartbeats WHERE timestamp > strftime('%s','now') - 600;
```

### 2. Check if the dashboard is running
```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9119/kanban
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8888/kanban
```
- 200 = running
- Connection refused = not started
- 401 = auth required (check session token)

### 3. Check if the cron job is creating tasks
```bash
hermes cron list  # verify job is enabled and last_status=ok
ps aux | grep hermes | grep kanban  # verify workers are running
```

### 4. Check task statuses
- All "done" + "archived" + stale "running" = no new tasks being created
- "running" tasks with recent heartbeats = workers are active
- "running" tasks with no heartbeats for 10+ min = workers may be stuck

### 5. Browser visual check
Navigate to `http://127.0.0.1:9119/kanban` in the browser tool. The React SPA renders the kanban board. Check:
- Column counts (Triage, Todo, Scheduled, Ready, In Progress, Done)
- Whether task cards appear in any column
- Worker lane groupings in In Progress

### 6. API auth issues
The Hermes dashboard at 9119 embeds `__HERMES_SESSION_TOKEN__` in the HTML and `__HERMES_AUTH_REQUIRED__=false`. The API at `/api/kanban/tasks` may still return 401 if the token isn't passed correctly. The React SPA handles auth internally via the embedded token — direct curl calls need the token.

## Host Identification Pitfall

Don't assume which machine you're running on based on user comments. A user saying "I guess you're on my phone now" doesn't mean the session is actually on the phone. Always verify:
```bash
ps aux | head -5  # macOS paths = Mac, /data/data/com.termux = Android
uname -a
```
