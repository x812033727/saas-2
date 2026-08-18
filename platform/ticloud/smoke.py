"""Launch smoke check for the zero-key demo path.

Run with an isolated database URL, for example:

    TICLOUD_DATABASE_URL=sqlite:///./smoke.db python -m ticloud.smoke

The check intentionally stays inside the platform package and avoids test-only
dependencies, so CI and release operators exercise the same code paths.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

DEFAULT_DATABASE_URL = "sqlite:///./ticloud.db"
HTTP_TIMEOUT_S = 5


@dataclass(frozen=True)
class SmokeSummary:
    jobs: int
    alerts: int
    lessons: int
    health_status: str
    http_base_url: str | None = None


def _fail(message: str) -> None:
    raise RuntimeError(message)


def _require_explicit_database_url() -> str:
    url = os.environ.get("TICLOUD_DATABASE_URL", "").strip()
    if not url:
        _fail("set TICLOUD_DATABASE_URL to an isolated smoke database")
    if url == DEFAULT_DATABASE_URL:
        _fail("TICLOUD_DATABASE_URL must not point at the default ticloud.db")
    return url


def _normalize_base_url(base_url: str | None = None) -> str | None:
    raw = base_url if base_url is not None else os.environ.get("TICLOUD_SMOKE_BASE_URL", "")
    raw = raw.strip()
    if not raw:
        return None
    return f"{raw.rstrip('/')}/"


def _http_get(base_url: str, path: str) -> str:
    url = urljoin(base_url, path.lstrip("/"))
    request = Request(url, headers={"accept": "application/json,text/plain"})
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        _fail(f"HTTP smoke {path} returned {exc.code}: {detail[:200]}")
    except URLError as exc:
        _fail(f"HTTP smoke {path} failed: {exc.reason}")
    except TimeoutError:
        _fail(f"HTTP smoke {path} timed out")


def _http_json(base_url: str, path: str):
    body = _http_get(base_url, path)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        _fail(f"HTTP smoke {path} did not return JSON")


def _check_http_surfaces(base_url: str, expected_names: set[str]) -> None:
    health_payload = _http_json(base_url, "/health")
    if health_payload.get("status") != "ok":
        _fail(f"HTTP health returned {health_payload!r}")

    templates_payload = _http_json(base_url, "/templates")
    if not isinstance(templates_payload, list) or not templates_payload:
        _fail("HTTP templates did not return any job templates")

    overview_payload = _http_json(base_url, "/overview")
    if not isinstance(overview_payload, list):
        _fail("HTTP overview did not return a job list")
    names = {item.get("name") for item in overview_payload if isinstance(item, dict)}
    missing = expected_names - names
    if missing:
        _fail(f"HTTP overview missing seeded jobs: {sorted(missing)}")

    metrics = _http_get(base_url, "/metrics")
    for marker in ("ticloud_runs_total", "ticloud_jobs", "ticloud_alerts_unacknowledged"):
        if marker not in metrics:
            _fail(f"HTTP metrics missing {marker}")


def run(base_url: str | None = None) -> SmokeSummary:
    """Seed the demo and verify the launch-critical surfaces are usable."""
    _require_explicit_database_url()
    http_base_url = _normalize_base_url(base_url)

    from sqlalchemy import select

    from .api.main import health
    from .db import get_session
    from .demo import DEMO_JOBS, seed
    from .metrics import render_metrics
    from .models import Alert, Job, Lesson
    from .templates import TEMPLATES

    status = health().get("status")
    if status != "ok":
        _fail(f"health check returned {status!r}")
    if not TEMPLATES:
        _fail("no job templates registered")

    seed()
    seed()

    session = get_session()
    try:
        expected_names = {spec["name"] for spec in DEMO_JOBS}
        jobs = session.scalars(select(Job)).all()
        names = {job.name for job in jobs}
        missing = expected_names - names
        if missing:
            _fail(f"demo seed missing jobs: {sorted(missing)}")

        dep_upgrade = next((job for job in jobs if job.name == "dep-upgrade"), None)
        if dep_upgrade is None or not dep_upgrade.paused:
            _fail("quality gate did not pause dep-upgrade")

        alerts = session.query(Alert).count()
        lessons = session.query(Lesson).count()
        if alerts <= 0:
            _fail("demo seed did not raise any alerts")
        if lessons <= 0:
            _fail("demo seed did not record any lessons")

        metrics = render_metrics(session)
        for marker in ("ticloud_runs_total", "ticloud_jobs", "ticloud_alerts_unacknowledged"):
            if marker not in metrics:
                _fail(f"metrics missing {marker}")

        if http_base_url is not None:
            _check_http_surfaces(http_base_url, expected_names)

        return SmokeSummary(
            jobs=len(jobs),
            alerts=alerts,
            lessons=lessons,
            health_status=status,
            http_base_url=http_base_url,
        )
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=None,
        help="Optional running API base URL to verify over HTTP.",
    )
    args = parser.parse_args(argv)

    try:
        summary = run(args.base_url)
    except Exception as exc:
        print(f"smoke FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"smoke OK {json.dumps(summary.__dict__, sort_keys=True)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
