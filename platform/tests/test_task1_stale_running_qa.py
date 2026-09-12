from datetime import datetime, timedelta, timezone

import pytest

from ticloud.config import settings
from ticloud.metrics import (
    STALE_RUNNING_GRACE_S,
    stale_running_counts_by_job,
    stale_running_run_ids,
)
from ticloud.models import Run, RunStatus

from test_scheduler import make_job


ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def hosted_mode(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name: str) -> dict[str, str]:
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN)
    assert tenant.status_code == 201, tenant.text
    key = client.post(
        f"/admin/tenants/{tenant.json()['id']}/keys",
        json={"name": "qa"},
        headers=ADMIN,
    )
    assert key.status_code == 201, key.text
    return {"Authorization": f"Bearer {key.json()['secret']}"}


def test_stale_running_detection_is_strict_and_ignores_non_running(session):
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    job = make_job(session, timeout_s=30)
    threshold = job.timeout_s + STALE_RUNNING_GRACE_S

    exactly_on_boundary = Run(
        job_id=job.id,
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=threshold),
        started_at=now - timedelta(seconds=threshold),
    )
    stale_running = Run(
        job_id=job.id,
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=threshold + 1),
        started_at=now - timedelta(seconds=threshold + 1),
    )
    stale_without_started_at = Run(
        job_id=job.id,
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=threshold + 2),
        started_at=None,
    )
    old_but_cancelled = Run(
        job_id=job.id,
        status=RunStatus.CANCELLED,
        scheduled_at=now - timedelta(seconds=threshold + 99),
    )
    session.add_all(
        [exactly_on_boundary, stale_running, stale_without_started_at, old_but_cancelled]
    )
    session.commit()

    assert stale_running_counts_by_job(session, [job.id], now=now) == {job.id: 2}
    assert stale_running_run_ids(session, [job.id], now=now) == {
        stale_running.id,
        stale_without_started_at.id,
    }


def test_overview_stale_running_counts_are_tenant_scoped(client, session, hosted_mode):
    auth_a = _mint_tenant(client, "team-a")
    auth_b = _mint_tenant(client, "team-b")
    job_a = client.post(
        "/jobs",
        json={"name": "a-job", "timeout_s": 10},
        headers=auth_a,
    ).json()
    job_b = client.post(
        "/jobs",
        json={"name": "b-job", "timeout_s": 10},
        headers=auth_b,
    ).json()
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            Run(
                job_id=job_a["id"],
                status=RunStatus.RUNNING,
                scheduled_at=now - timedelta(seconds=30),
                started_at=now - timedelta(seconds=30),
            ),
            Run(
                job_id=job_b["id"],
                status=RunStatus.RUNNING,
                scheduled_at=now - timedelta(seconds=30),
                started_at=now - timedelta(seconds=30),
            ),
        ]
    )
    session.commit()

    overview_a = client.get("/overview", headers=auth_a).json()
    overview_b = client.get("/overview", headers=auth_b).json()

    assert [(job["name"], job["stale_running_runs"]) for job in overview_a] == [
        ("a-job", 1)
    ]
    assert [(job["name"], job["stale_running_runs"]) for job in overview_b] == [
        ("b-job", 1)
    ]
