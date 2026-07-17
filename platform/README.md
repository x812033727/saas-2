# ticloud

Agent-native cron scheduler with quality gates: run autonomous AI agents
on a schedule, score every run, catch drift, and turn failures into
regression tests.

Full documentation, screenshots, and quick start:
https://github.com/x812033727/saas-2

```bash
pip install -e "platform[dev]"
uvicorn ticloud.api.main:app &          # API + dashboard on :8000/ui/
python -m ticloud.scheduler.worker &    # scheduler + executor
python -m ticloud.demo                  # zero-API-key showcase seed
```

Apache-2.0.
