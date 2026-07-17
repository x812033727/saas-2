# Contributing to Ti Cloud

Thanks for your interest! This project is young and moving fast — small,
focused PRs land quickest.

## Dev setup

```bash
pip install -e "platform[dev]"

# API (serves the dashboard at /ui/)
uvicorn ticloud.api.main:app --reload

# Scheduler + executor (separate terminal)
python -m ticloud.scheduler.worker

# Seed demo data (no API keys needed)
python -m ticloud.demo
```

SQLite is the zero-config default; set `TICLOUD_DATABASE_URL` to point at
Postgres (`postgresql+psycopg2://...`) to match production.

## Tests

```bash
cd platform && python -m pytest
```

Every PR must keep the suite green. New behavior needs a test — the
offline engine's payload knobs (`fail_at`, `flaky_fail_at`,
`cost_multiplier`, `sleep_s`) exist precisely to make failure scenarios
testable without credentials.

## Guidelines

- **Guards fail closed.** Budget/timeout/scoring code paths must never
  let an unscored or unguarded run slip through on error.
- **The frontend stays no-build.** Plain HTML/CSS/JS served by FastAPI;
  check syntax with `node --check platform/ticloud/web/app.js`.
- **Self-host stays zero-dependency.** Features requiring external APIs
  (LLM judge, embedding clustering) must degrade gracefully when
  credentials are absent.
- Run `python -m ticloud.eval.cli run` if your change touches engines or
  scorers — CI blocks regressions against the eval-set.

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0.
