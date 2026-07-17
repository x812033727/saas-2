"""Wave 1 — performance & correctness hardening.

Covers the SQL-aggregated spend path, the overview N+1 fix, the usage
window bound, and the additive index micro-migration on upgraded DBs.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text

from test_scheduler import make_job
from ticloud.billing import month_to_date_cost
from ticloud.db import engine, init_db
from ticloud.models import Job, Run, RunStatus


def _run(session, job_id, *, cost, started=None, scheduled=None, status=RunStatus.SUCCEEDED):
    session.add(
        Run(
            job_id=job_id,
            status=status,
            scheduled_at=scheduled or datetime.now(timezone.utc),
            started_at=started,
            cost_usd=cost,
        )
    )
    session.commit()


# --- spend aggregation (SQL) -------------------------------------------------


def test_month_to_date_sums_only_this_month(session):
    job = make_job(session)
    now = datetime.now(timezone.utc)
    _run(session, job.id, cost=1.0, scheduled=now)
    _run(session, job.id, cost=2.0, scheduled=now)
    _run(session, job.id, cost=99.0, scheduled=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert month_to_date_cost(session, [job.id]) == pytest.approx(3.0)


def test_month_to_date_anchors_on_started_at_over_scheduled(session):
    """A run scheduled this month but started last month counts by start."""
    job = make_job(session)
    now = datetime.now(timezone.utc)
    last_month = datetime(2000, 6, 15, tzinfo=timezone.utc)
    _run(session, job.id, cost=5.0, scheduled=now, started=last_month)
    # started_at (old) wins over scheduled_at (now) → excluded this month.
    assert month_to_date_cost(session, [job.id]) == 0.0
    # A never-started run falls back to scheduled_at → counted.
    _run(session, job.id, cost=4.0, scheduled=now, started=None, status=RunStatus.QUEUED)
    assert month_to_date_cost(session, [job.id]) == pytest.approx(4.0)


def test_month_to_date_empty_and_missing(session):
    assert month_to_date_cost(session, []) == 0.0
    job = make_job(session)
    assert month_to_date_cost(session, [job.id]) == 0.0  # no runs


# --- overview: latest run per job in one query, correct value ----------------


def test_overview_returns_latest_run_per_job(session, client):
    job = make_job(session, name="j1")
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new = datetime(2020, 6, 1, tzinfo=timezone.utc)
    _run(session, job.id, cost=1.0, scheduled=old)
    _run(session, job.id, cost=2.0, scheduled=new)  # newest
    _run(session, job.id, cost=1.5, scheduled=datetime(2020, 3, 1, tzinfo=timezone.utc))

    rows = client.get("/overview").json()
    row = next(r for r in rows if r["name"] == "j1")
    assert row["last_run"]["cost_usd"] == pytest.approx(2.0)


def test_overview_job_without_runs(session, client):
    make_job(session, name="empty")
    row = next(r for r in client.get("/overview").json() if r["name"] == "empty")
    assert row["last_run"] is None


# --- usage window bound ------------------------------------------------------


def test_usage_excludes_runs_older_than_window(session, client):
    job = make_job(session)
    now = datetime.now(timezone.utc)
    _run(session, job.id, cost=3.0, scheduled=now)
    _run(session, job.id, cost=9.0, scheduled=datetime(2000, 1, 1, tzinfo=timezone.utc))
    months = client.get("/usage").json()["months"]
    # Only the current month is within the 12-month window.
    assert len(months) == 1
    assert months[0]["cost_usd"] == pytest.approx(3.0)


# --- additive index micro-migration (drift fix) ------------------------------


def test_init_db_backfills_missing_indexes():
    """Simulate a DB upgraded before the indexes existed: drop them, then
    init_db must recreate them (create_all skips existing tables' indexes)."""
    targets = {
        "jobs": {"ix_jobs_tenant_id", "ix_jobs_next_run_at"},
        "tenants": {"ix_tenants_stripe_customer_id"},
        "runs": {"ix_runs_job_scheduled"},
    }
    with engine.begin() as conn:
        for names in targets.values():
            for name in names:
                conn.execute(text(f"DROP INDEX IF EXISTS {name}"))

    insp = inspect(engine)
    for table, names in targets.items():
        present = {ix["name"] for ix in insp.get_indexes(table)}
        assert not (names & present), f"precondition: {names} should be dropped"

    init_db()  # runs the micro-migration

    insp = inspect(engine)
    for table, names in targets.items():
        present = {ix["name"] for ix in insp.get_indexes(table)}
        assert names <= present, f"{names - present} not recreated on {table}"
    init_db()  # idempotent


def test_composite_index_covers_job_and_scheduled(session):
    insp = inspect(engine)
    runs_ix = {ix["name"]: ix["column_names"] for ix in insp.get_indexes("runs")}
    assert runs_ix.get("ix_runs_job_scheduled") == ["job_id", "scheduled_at"]
