from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from test_scheduler import make_job
from ticloud.config import settings
from ticloud.metrics import render_metrics
from ticloud.models import Run, RunStatus

ADMIN = {"Authorization": "Bearer admin-secret"}


def _metric_value(body: str, name: str) -> float:
    prefix = f"{name} "
    for line in body.splitlines():
        if line.startswith(prefix):
            return float(line.removeprefix(prefix))
    raise AssertionError(f"metric {name} not found")


def _mint_tenant(client, name: str):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()
    key = client.post(
        f"/admin/tenants/{tenant['id']}/keys", json={"name": "qa"}, headers=ADMIN
    ).json()
    return tenant, {"Authorization": f"Bearer {key['secret']}"}


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (RunStatus.SUCCEEDED, None),
        (RunStatus.FAILED, "engine blew up"),
        (RunStatus.TIMED_OUT, "deadline exceeded"),
        (RunStatus.BUDGET_EXCEEDED, "budget exhausted"),
        (RunStatus.CANCELLED, "cancelled by user"),
    ],
)
def test_qa_rerun_accepts_every_terminal_status_without_mutating_source(
    session, client, status, error
):
    job = make_job(session)
    source = Run(
        job_id=job.id,
        status=status,
        attempt=3,
        result={"original": "must stay"},
        error=error,
        finished_at=datetime.now(timezone.utc),
    )
    session.add(source)
    session.commit()

    resp = client.post(f"/runs/{source.id}/rerun")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] != source.id
    assert body["job_id"] == job.id
    assert body["status"] == "queued"
    assert body["attempt"] == 1
    assert body["result"]["rerun_of"] == source.id
    if error is None:
        assert "previous_error" not in body["result"]
    else:
        assert body["result"]["previous_error"] == error

    session.expire_all()
    original = session.get(Run, source.id)
    assert original.status == status
    assert original.attempt == 3
    assert original.result == {"original": "must stay"}
    assert original.error == error


@pytest.mark.parametrize(
    "status",
    [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.AWAITING_APPROVAL],
)
def test_qa_rerun_rejects_non_terminal_runs_and_creates_no_child(session, client, status):
    job = make_job(session)
    run = Run(
        job_id=job.id,
        status=status,
        started_at=datetime.now(timezone.utc) if status == RunStatus.RUNNING else None,
        approval_state="pending" if status == RunStatus.AWAITING_APPROVAL else None,
    )
    session.add(run)
    session.commit()
    before = session.scalar(select(func.count(Run.id)))

    resp = client.post(f"/runs/{run.id}/rerun")
    assert resp.status_code == 409, resp.text
    assert status.value in resp.json()["detail"]
    assert session.scalar(select(func.count(Run.id))) == before


def test_qa_rerun_is_tenant_scoped(client, hosted, session):
    _, auth_a = _mint_tenant(client, "team-a")
    _, auth_b = _mint_tenant(client, "team-b")
    job = client.post("/jobs", json={"name": "a-job"}, headers=auth_a).json()
    run = client.post(f"/jobs/{job['id']}/trigger", headers=auth_a).json()

    source = session.get(Run, run["id"])
    source.status = RunStatus.FAILED
    source.error = "tenant-private failure"
    source.finished_at = datetime.now(timezone.utc)
    session.commit()

    assert client.post(f"/runs/{run['id']}/rerun").status_code == 401
    assert client.post(f"/runs/{run['id']}/rerun", headers=auth_b).status_code == 404
    resp = client.post(f"/runs/{run['id']}/rerun", headers=auth_a)
    assert resp.status_code == 201, resp.text
    assert resp.json()["result"]["rerun_of"] == run["id"]


def test_qa_rerun_obeys_tenant_budget(client, hosted, session):
    tenant, auth = _mint_tenant(client, "budgeted")
    client.put(
        f"/admin/tenants/{tenant['id']}/budget",
        json={"monthly_budget_usd": 1.0},
        headers=ADMIN,
    )
    job = client.post("/jobs", json={"name": "budget-job"}, headers=auth).json()
    source = Run(
        job_id=job["id"],
        status=RunStatus.FAILED,
        error="fixable failure",
        finished_at=datetime.now(timezone.utc),
    )
    spend = Run(
        job_id=job["id"],
        status=RunStatus.SUCCEEDED,
        scheduled_at=datetime.now(timezone.utc),
        cost_usd=1.5,
    )
    session.add_all([source, spend])
    session.commit()

    resp = client.post(f"/runs/{source.id}/rerun", headers=auth)
    assert resp.status_code == 402, resp.text
    assert "budget" in resp.json()["detail"]


def test_qa_metrics_queue_age_uses_oldest_run_and_clamps_future_queue(session):
    job = make_job(session)
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            Run(job_id=job.id, status=RunStatus.QUEUED, scheduled_at=now - timedelta(seconds=20)),
            Run(job_id=job.id, status=RunStatus.QUEUED, scheduled_at=now - timedelta(seconds=120)),
            Run(job_id=job.id, status=RunStatus.QUEUED, scheduled_at=now + timedelta(seconds=300)),
        ]
    )
    session.commit()

    age = _metric_value(render_metrics(session), "ticloud_oldest_queued_run_age_seconds")
    assert 120 <= age < 180


def test_qa_metrics_future_only_queue_age_is_never_negative(session):
    job = make_job(session)
    session.add(
        Run(
            job_id=job.id,
            status=RunStatus.QUEUED,
            scheduled_at=datetime.now(timezone.utc) + timedelta(seconds=300),
        )
    )
    session.commit()

    assert _metric_value(render_metrics(session), "ticloud_oldest_queued_run_age_seconds") == 0.0


def test_qa_metrics_running_age_prefers_oldest_started_run(session):
    job = make_job(session)
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            Run(
                job_id=job.id,
                status=RunStatus.RUNNING,
                scheduled_at=now - timedelta(seconds=999),
                started_at=now - timedelta(seconds=20),
            ),
            Run(
                job_id=job.id,
                status=RunStatus.RUNNING,
                scheduled_at=now - timedelta(seconds=100),
                started_at=now - timedelta(seconds=90),
            ),
        ]
    )
    session.commit()

    age = _metric_value(render_metrics(session), "ticloud_oldest_running_run_age_seconds")
    assert 90 <= age < 140
