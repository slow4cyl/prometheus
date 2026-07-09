# Fresh-machine bootstrap

Target: a Linux box (reference: CachyOS/Arch), Python 3.11–3.14, ideally one
CUDA GPU for a free local worker lane (optional — any OpenAI-compatible
endpoint works). Everything installs under `~/.hermes/`.

## 1. Install the substrate (hermes-agent)

```bash
git clone https://github.com/NousResearch/hermes-agent ~/.hermes/hermes-agent
cd ~/.hermes/hermes-agent
python -m venv venv && venv/bin/pip install -e .
# Python 3.14 note: upstream caps requires-python at <3.14 until its Rust
# deps ship cp314 wheels. On 3.14: pip install -e . --no-deps into a venv
# that already carries the deps, and apply fork-patches/ (see below).
```

**Carried patches** — check `fork-patches/upstream-prs/README.md` first: every
patch has an open upstream PR, and each merged PR means one less patch to
apply. Until then, apply the ones you need (`git apply`); on Python 3.14 the
py314 trio is required, the rest are robustness fixes for fleet-scale kanban.

## 2. Configuration

```bash
cp config/config.example.yaml ~/.hermes/config.yaml
mkdir -p ~/.hermes/profiles/{a1,interactive}
cp config/profiles/a1.yaml          ~/.hermes/profiles/a1/config.yaml
cp config/profiles/interactive.yaml ~/.hermes/profiles/interactive/config.yaml
```

Edit `~/.hermes/config.yaml`:
- `model.provider` / `model.default` — your primary worker model (the example
  routes to OpenRouter; set your key via `~/.hermes/.env`).
- `providers.agents-a1` — point at your local OpenAI-compatible endpoint, or
  remove it (and the a1 profile) if you run API-only.

## 3. Plugins — the update-proof policy layer

```bash
cp -r plugins/* ~/.hermes/plugins/
```

**Critical rule: worker profiles are self-contained.** A plugin enabled only
in the main config silently does not exist for workers. For EVERY worker
profile:

```bash
for prof in a1 interactive; do
  mkdir -p ~/.hermes/profiles/$prof/plugins
  for p in prometheus-guard prometheus-prompt-policy prometheus-runtime-tuning; do
    ln -sf ~/.hermes/plugins/$p ~/.hermes/profiles/$prof/plugins/$p
  done
done
```

(The example profile configs already list all three under `plugins.enabled`.)
After any hermes upgrade, check `~/.hermes/plugins/*/PATCH_FAILED*` — a marker
means upstream drifted under a sentinel and that patch is inert (stock
behavior) until refreshed. The system keeps running either way.

## 4. The research layer

```bash
cp -r scripts ~/.hermes/scripts
cp -r skills  ~/.hermes/skills           # kanban-worker + prometheus-* skills
mkdir -p ~/.hermes/experiments ~/.hermes/logs ~/.hermes/backups

# knowledge DB (empty, schema only)
sqlite3 ~/.hermes/prometheus.db < schema/prometheus.schema.sql
# kanban.db is created by hermes itself on first dispatcher run
```

Then the cron surface — review it before importing (it's ~90 jobs; every
schedule uses staggered offsets so lanes never fire on the same minute):

```bash
cp cron/jobs.json ~/.hermes/cron/jobs.json
```

Start small if you prefer: `enabled: false` everything except
`task-refiller`, `task-janitor-v2`, the backups, and `db-reconciliation
-monitor`, then switch lanes on as you watch the dashboard.

## 5. Services

```bash
cp systemd/*.service ~/.config/systemd/user/
# EDIT PATHS in each unit (home dir, venv, model path) before enabling.
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway hermes-dashboard
# Optional local worker lane (needs a big GPU; reference: RTX 5090 32GB):
#   agents-a1-fp4.service  — vLLM serving a 30B-class model in FP4
#   agents-a1-router.service — stamps ready tasks onto the local lane
```

GPU sizing that survived contact with reality (reference deployment): vLLM
`--gpu-memory-utilization 0.82` (leave ~3 GB for worker CUDA contexts),
`Restart=always` + `RestartSec=45` + `StartLimitIntervalSec=0` **in [Unit]**
— vLLM's API server exits rc=0 when the engine dies, so `on-failure` never
fires.

## 6. First-run sanity checklist

```bash
systemctl --user is-active hermes-gateway        # active
ls ~/.hermes/plugins/*/PATCH_FAILED* 2>/dev/null # no output = plugins live
grep prometheus ~/.hermes/logs/agent.log | tail  # "prompt policy applied", "wrapped", "rebound"
sqlite3 ~/.hermes/kanban.db "SELECT COUNT(*) FROM tasks;"   # grows once refiller runs
```

Dashboard: `dashboard/prometheus_dashboard_v2.py` serves the live fleet view
(default :8889) — run it directly or via a cron `no_agent` job.

## Operational rules learned the hard way

1. **Never run the hermes test suite against the real home.** Five upstream
   tests write the live `~/.hermes/config.yaml` (fix submitted upstream).
   Always `HERMES_HOME=$(mktemp -d) pytest …`.
2. **Never edit a working dial on an assumption.** Change what the task
   requires; smallest fix first.
3. **Wire fixes into cron/gates, not manual tools.** On an automated system a
   report-only script is a fix that doesn't exist.
4. **Verify local-lane usage by connections** (`ss -tn | grep :8001`), never
   by task counts or GPU blips.
5. **Back up both DBs on schedule** (jobs included) and treat
   `db-reconciliation-monitor` alerts as real until proven benign.
