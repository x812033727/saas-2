# Ti Cloud

**An autonomous AI dev team on a schedule** — it patrols your repos, ships
quality-gated PRs, and never forgets what it learned.

Ti Cloud is the platform layer around the open-source
[Ti](https://github.com/x812033727/Ti) multi-expert engine (PM / engineer /
senior engineer / QA collaborating end-to-end). It adds what unattended,
recurring agent work actually needs:

- **Agent-native cron/loop scheduling** — cron or interval triggers with
  per-run **cost budgets**, **timeouts**, and **failure-context retries**
  (a retry carries the previous error so the next attempt can adapt).
- **Structured run traces** — every role turn and tool call recorded live
  (role, cost, tokens, timing), streamable to a UI.
- **Quality gates** — every finished run is scored automatically:
  rule-based scorers (completion, **trajectory health** — stuck-loop and
  review-verdict checks that catch "answer looked fine, process was broken"
  silent failures — and cost anomaly vs the job's own history) plus an
  optional Claude **LLM judge** (`pip install "platform[judge]"` +
  `ANTHROPIC_API_KEY`, config `{"scorers": {"judge": {"enabled": true}}}`).
  Score below the job's `score_threshold` → alert (webhook via
  `TICLOUD_WEBHOOK_URL`, Slack-compatible) and, with
  `on_low_score: "pause"`, the schedule **auto-pauses**.
- **Drift view** — score and cost trends per job with the gate drawn in,
  so slow degradation is visible before it becomes an incident.
- **Knowledge flywheel** *(Phase 3)* — failures cluster into eval cases;
  lessons persist across runs, so nightly patrols get smarter over time.

## Quick start

```bash
# Full stack (Postgres + API + worker):
docker compose -f deploy/docker-compose.yml up

# Or local dev (SQLite, zero config):
pip install -e "platform[dev]"
uvicorn ticloud.api.main:app --reload &        # API on :8000
python -m ticloud.scheduler.worker             # scheduler + executor
```

Then open the dashboard at **http://localhost:8000/ui/** — jobs overview
with live status badges, per-job cost trend, and a step-by-step trace for
every run (role, cost, tokens, timing), refreshing live while a run is
in flight.

Create a nightly patrol job (the `offline` engine simulates a full Ti
workshop with no API keys — swap to `ti` for the real engine):

```bash
curl -X POST localhost:8000/jobs -H 'content-type: application/json' -d '{
  "name": "nightly-patrol",
  "engine": "offline",
  "cron": "0 2 * * *",
  "budget_usd": 2.0,
  "timeout_s": 1800
}'

curl -X POST localhost:8000/jobs/<job_id>/trigger   # fire once, right now
curl localhost:8000/runs/<run_id>                   # full step-by-step trace
```

## Layout

```
platform/ticloud/
  scheduler/   cron computation, DB-backed queue (SKIP LOCKED), worker loop
  engine/      AgentEngine protocol, offline demo engine, Ti adapter
  api/         FastAPI management API (jobs, runs, trigger, pause/resume)
  web/         no-build dashboard (jobs, run history, live trace) at /ui/
  models.py    Job / Run / RunStep (structured trace)
deploy/        Dockerfile + docker-compose (Postgres + API + worker)
docs/PLAN.md   Product & roadmap plan (zh-TW)
```

## Tests

```bash
cd platform && python -m pytest
```

Covers schedule math, queue claim semantics, budget/timeout guards,
retry-with-context, and the API end-to-end.

## Roadmap

See [docs/PLAN.md](docs/PLAN.md) — Phase 1 (this skeleton): scheduling +
tracing; Phase 2: eval gates, alerting, run-over-run drift; Phase 3:
knowledge flywheel, auto eval-mining, CI gate.
