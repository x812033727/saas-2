"""Per-tenant monthly spend caps: enforcement, admin control, usage.

Builds on the hosted-mode fixtures. A tenant with no cap (the default,
and all self-host jobs) is never blocked — proven alongside the enforced
cases.
"""

import pytest

from test_worker import run_job_once
from ticloud.billing import month_to_date_cost, tenant_over_budget
from ticloud.config import settings
from ticloud.db import get_session
from ticloud.models import Alert, Job, Tenant
from ticloud.scheduler.queue import claim_next_run, enqueue_due_jobs, enqueue_manual
from ticloud.scheduler.worker import execute_run

ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()
    key = client.post(
        f"/admin/tenants/{tenant['id']}/keys", json={"name": "ci"}, headers=ADMIN
    ).json()
    return tenant, {"Authorization": f"Bearer {key['secret']}"}


def _spend(session, job_id, usd):
    """Attribute a finished run's cost to a job so month-to-date rises."""
    from datetime import datetime, timezone

    from ticloud.models import Run, RunStatus

    run = Run(
        job_id=job_id,
        status=RunStatus.SUCCEEDED,
        scheduled_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        cost_usd=usd,
    )
    session.add(run)
    session.commit()


# --- admin budget control ----------------------------------------------------


def test_set_and_clear_budget(client, hosted):
    tenant, _ = _mint_tenant(client, "acme")
    assert tenant["monthly_budget_usd"] is None

    updated = client.put(
        f"/admin/tenants/{tenant['id']}/budget", json={"monthly_budget_usd": 25.0}, headers=ADMIN
    ).json()
    assert updated["monthly_budget_usd"] == 25.0

    cleared = client.put(
        f"/admin/tenants/{tenant['id']}/budget", json={"monthly_budget_usd": None}, headers=ADMIN
    ).json()
    assert cleared["monthly_budget_usd"] is None

    assert client.put(
        "/admin/tenants/nope/budget", json={"monthly_budget_usd": 5}, headers=ADMIN
    ).status_code == 404
    # negative rejected
    assert client.put(
        f"/admin/tenants/{tenant['id']}/budget", json={"monthly_budget_usd": -1}, headers=ADMIN
    ).status_code == 422


# --- manual trigger enforcement ----------------------------------------------


def test_trigger_blocked_over_budget(client, hosted, session):
    tenant, auth = _mint_tenant(client, "acme")
    client.put(
        f"/admin/tenants/{tenant['id']}/budget", json={"monthly_budget_usd": 1.0}, headers=ADMIN
    )
    job = client.post("/jobs", json={"name": "j"}, headers=auth).json()

    # Under budget: trigger works.
    assert client.post(f"/jobs/{job['id']}/trigger", headers=auth).status_code == 201
    _spend(session, job["id"], 1.5)  # push month-to-date over the $1 cap
    # Over budget: 402.
    resp = client.post(f"/jobs/{job['id']}/trigger", headers=auth)
    assert resp.status_code == 402
    assert "budget" in resp.json()["detail"]


def test_no_cap_never_blocks(client, hosted, session):
    _, auth = _mint_tenant(client, "acme")  # no budget set
    job = client.post("/jobs", json={"name": "j"}, headers=auth).json()
    _spend(session, job["id"], 999.0)
    assert client.post(f"/jobs/{job['id']}/trigger", headers=auth).status_code == 201


def test_self_host_jobs_never_blocked(session):
    """auth_mode=off jobs have no tenant, so quota never applies."""
    run = run_job_once(session)  # unowned job, executes normally
    assert run.status.value == "succeeded"


# --- scheduler enforcement ---------------------------------------------------


def test_scheduler_skips_over_budget_and_alerts_once(client, hosted, session):
    from datetime import datetime, timezone

    tenant, auth = _mint_tenant(client, "acme")
    client.put(
        f"/admin/tenants/{tenant['id']}/budget", json={"monthly_budget_usd": 1.0}, headers=ADMIN
    )
    # An interval job that's due now.
    job = client.post(
        "/jobs", json={"name": "sched", "interval_seconds": 10}, headers=auth
    ).json()
    _spend(session, job["id"], 2.0)  # over the $1 cap

    job_row = session.get(Job, job["id"])
    job_row.next_run_at = datetime(2020, 1, 1, tzinfo=timezone.utc)  # force due
    session.commit()

    created = enqueue_due_jobs(session)
    assert created == []  # skipped, no run enqueued
    # next_run_at still advanced (job resumes automatically next period).
    assert session.get(Job, job["id"]).next_run_at.year > 2020

    alerts = session.query(Alert).filter_by(job_id=job["id"], kind="quota_exceeded").all()
    assert len(alerts) == 1

    # A second due fire doesn't pile on another unacked alert.
    job_row = session.get(Job, job["id"])
    job_row.next_run_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.commit()
    enqueue_due_jobs(session)
    assert session.query(Alert).filter_by(job_id=job["id"], kind="quota_exceeded").count() == 1


def test_scheduler_runs_under_budget(client, hosted, session):
    from datetime import datetime, timezone

    tenant, auth = _mint_tenant(client, "acme")
    client.put(
        f"/admin/tenants/{tenant['id']}/budget", json={"monthly_budget_usd": 100.0}, headers=ADMIN
    )
    job = client.post(
        "/jobs", json={"name": "sched", "interval_seconds": 10}, headers=auth
    ).json()
    session.get(Job, job["id"]).next_run_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.commit()
    assert len(enqueue_due_jobs(session)) == 1


# --- usage reports the cap ---------------------------------------------------


def test_usage_reports_budget_and_over_flag(client, hosted, session):
    tenant, auth = _mint_tenant(client, "acme")
    client.put(
        f"/admin/tenants/{tenant['id']}/budget", json={"monthly_budget_usd": 5.0}, headers=ADMIN
    )
    job = client.post("/jobs", json={"name": "j"}, headers=auth).json()
    _spend(session, job["id"], 6.0)

    usage = client.get("/usage", headers=auth).json()
    assert usage["monthly_budget_usd"] == 5.0
    assert usage["current_month_cost_usd"] == 6.0
    assert usage["over_budget"] is True


# --- billing unit ------------------------------------------------------------


def test_month_to_date_only_counts_current_month(session):
    from datetime import datetime, timezone

    from ticloud.models import Run, RunStatus

    job = Job(name="j")
    session.add(job)
    session.commit()
    # A run stamped last month must not count toward this month.
    old = Run(
        job_id=job.id,
        status=RunStatus.SUCCEEDED,
        scheduled_at=datetime(2020, 1, 15, tzinfo=timezone.utc),
        started_at=datetime(2020, 1, 15, tzinfo=timezone.utc),
        cost_usd=99.0,
    )
    session.add(old)
    session.commit()
    _spend(session, job.id, 3.0)  # this month
    assert month_to_date_cost(session, [job.id]) == 3.0
