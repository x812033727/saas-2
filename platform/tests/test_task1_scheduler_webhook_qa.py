from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from test_api import create_job
from test_scheduler import make_job
from ticloud.config import Settings, settings
from ticloud.models import Run, RunStatus, Tenant
from ticloud.scheduler.queue import enqueue_due_jobs, enqueue_manual
from ticloud.validation import validate_webhook_url


@pytest.mark.parametrize(
    "url",
    [
        "https://token@hooks.example/alert",
        "https://user:pass@hooks.example/alert",
        "https://hooks.example:abc/alert",
        "https://hooks.example:99999/alert",
        "https://hooks.example\\@evil.example/alert",
    ],
)
def test_qa_rejects_secret_bearing_or_bad_port_webhook_urls(url):
    with pytest.raises(ValueError):
        validate_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://token@hooks.example/alert",
        "https://hooks.example:99999/alert",
    ],
)
def test_qa_webhook_rejection_is_enforced_on_config_and_api_paths(client, monkeypatch, url):
    monkeypatch.setenv("TICLOUD_WEBHOOK_URL", url)
    with pytest.raises(ValidationError):
        Settings()

    create_resp = client.post(
        "/jobs",
        json={
            "name": "qa-bad-webhook-create",
            "engine": "offline",
            "cron": "0 2 * * *",
            "webhook_url": url,
        },
    )
    assert create_resp.status_code == 422, create_resp.text

    job = create_job(client, name="qa-bad-webhook-patch")
    patch_resp = client.patch(f"/jobs/{job['id']}", json={"webhook_url": url})
    assert patch_resp.status_code == 422, patch_resp.text

    monkeypatch.setattr(settings, "admin_token", "qa-admin-secret")
    tenant = client.post(
        "/admin/tenants",
        json={"name": "qa-bad-webhook-tenant"},
        headers={"Authorization": "Bearer qa-admin-secret"},
    ).json()
    tenant_resp = client.patch(
        f"/admin/tenants/{tenant['id']}",
        json={"webhook_url": url},
        headers={"Authorization": "Bearer qa-admin-secret"},
    )
    assert tenant_resp.status_code == 422, tenant_resp.text


def test_qa_scheduled_overlap_block_is_scoped_to_the_same_job(session):
    now = datetime.now(timezone.utc)
    blocked = make_job(
        session,
        name="qa-blocked-active",
        interval_seconds=60,
        next_run_at=now - timedelta(minutes=1),
    )
    allowed = make_job(
        session,
        name="qa-allowed-sibling",
        interval_seconds=60,
        next_run_at=now - timedelta(minutes=1),
    )
    session.add(
        Run(
            job_id=blocked.id,
            status=RunStatus.RUNNING,
            scheduled_at=now - timedelta(minutes=5),
        )
    )
    session.commit()

    created = enqueue_due_jobs(session, now=now)

    assert [run.job_id for run in created] == [allowed.id]
    assert session.query(Run).filter(Run.job_id == blocked.id).count() == 1
    assert session.query(Run).filter(Run.job_id == allowed.id).count() == 1
    session.refresh(blocked)
    session.refresh(allowed)
    assert blocked.next_run_at > now
    assert allowed.next_run_at > now


def test_qa_manual_trigger_is_not_blocked_by_scheduled_overlap_guard(session):
    now = datetime.now(timezone.utc)
    job = make_job(
        session,
        name="qa-manual-still-allowed",
        interval_seconds=60,
        next_run_at=now - timedelta(minutes=1),
    )
    session.add(
        Run(
            job_id=job.id,
            status=RunStatus.RUNNING,
            scheduled_at=now - timedelta(minutes=5),
        )
    )
    session.commit()

    assert enqueue_due_jobs(session, now=now) == []

    manual = enqueue_manual(session, job)

    assert manual.status == RunStatus.QUEUED
    assert session.query(Run).filter(Run.job_id == job.id).count() == 2
