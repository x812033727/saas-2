"""Wave 4 backend — per-tenant webhook routing + concurrency caps."""

from datetime import datetime, timedelta, timezone

import pytest

from test_api import create_job
from test_scheduler import make_job
from ticloud.config import settings
from ticloud.eval import notify
from ticloud.models import Run, RunStatus, Tenant
from ticloud.scheduler.queue import claim_next_run, enqueue_manual, running_count

ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def admin_mode(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")


# --- B2 per-tenant webhook routing -------------------------------------------


def test_webhook_resolution_precedence(session):
    t = Tenant(name="t", webhook_url="https://tenant.example")
    session.add(t)
    session.commit()
    j_tenant = make_job(session, name="j1", tenant_id=t.id)
    j_override = make_job(session, name="j2", tenant_id=t.id, webhook_url="https://job.example")
    j_none = make_job(session, name="j3")

    assert notify._resolve_webhook_url(session, j_tenant.id) == "https://tenant.example"
    assert notify._resolve_webhook_url(session, j_override.id) == "https://job.example"
    assert notify._resolve_webhook_url(session, j_none.id) is None  # global unset


def test_raise_alert_delivers_to_resolved_url(session, monkeypatch):
    captured = {}
    monkeypatch.setattr(notify, "_push_webhook", lambda alert, url: captured.update(url=url))
    t = Tenant(name="t", webhook_url="https://tenant.example")
    session.add(t)
    session.commit()
    job = make_job(session, name="j", tenant_id=t.id)
    notify.raise_alert(session, job.id, kind="low_score", message="x")
    assert captured["url"] == "https://tenant.example"


def test_global_webhook_fallback(session, monkeypatch):
    monkeypatch.setattr(settings, "webhook_url", "https://global.example")
    job = make_job(session)  # no tenant, no job override
    assert notify._resolve_webhook_url(session, job.id) == "https://global.example"


def test_job_webhook_url_via_api(session, client):
    job = create_job(client, webhook_url="https://job.example")
    assert job["webhook_url"] == "https://job.example"
    cleared = client.patch(f"/jobs/{job['id']}", json={"webhook_url": None}).json()
    assert cleared["webhook_url"] is None


def test_create_job_rejects_invalid_webhook_url(client):
    resp = client.post(
        "/jobs",
        json={
            "name": "bad-webhook",
            "engine": "offline",
            "cron": "0 2 * * *",
            "webhook_url": "ftp://job.example",
        },
    )
    assert resp.status_code == 422


def test_patch_job_rejects_invalid_webhook_url(client):
    job = create_job(client)
    resp = client.patch(f"/jobs/{job['id']}", json={"webhook_url": "not-a-url"})
    assert resp.status_code == 422


# --- B7 concurrency caps -----------------------------------------------------


def test_global_cap_blocks_and_lifts(session, monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_runs", 1)
    job = make_job(session)
    session.add(Run(job_id=job.id, status=RunStatus.RUNNING))
    session.commit()
    enqueue_manual(session, job)
    assert claim_next_run(session) is None  # at global cap

    monkeypatch.setattr(settings, "max_concurrent_runs", 0)  # unlimited
    assert claim_next_run(session) is not None


def test_per_tenant_cap_skips_busy_tenant(session):
    busy = Tenant(name="busy", max_concurrent_runs=1)
    free = Tenant(name="free")
    session.add_all([busy, free])
    session.commit()
    jb = make_job(session, name="jb", tenant_id=busy.id)
    jf = make_job(session, name="jf", tenant_id=free.id)

    session.add(Run(job_id=jb.id, status=RunStatus.RUNNING))  # busy is at its cap of 1
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session.add(Run(job_id=jb.id, status=RunStatus.QUEUED, scheduled_at=base))  # older
    session.add(Run(job_id=jf.id, status=RunStatus.QUEUED, scheduled_at=base + timedelta(seconds=1)))
    session.commit()

    # Head-of-line (busy tenant) is skipped; the free tenant's run is claimed.
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.job_id == jf.id


def test_running_count_scoping(session):
    t = Tenant(name="t")
    session.add(t)
    session.commit()
    jt = make_job(session, name="jt", tenant_id=t.id)
    jo = make_job(session, name="jo")
    session.add(Run(job_id=jt.id, status=RunStatus.RUNNING))
    session.add(Run(job_id=jo.id, status=RunStatus.RUNNING))
    session.commit()
    assert running_count(session) == 2
    assert running_count(session, t.id) == 1


# --- admin tenant settings ---------------------------------------------------


def test_admin_patch_tenant_settings(client, admin_mode):
    t = client.post("/admin/tenants", json={"name": "acme"}, headers=ADMIN).json()
    assert t["webhook_url"] is None and t["max_concurrent_runs"] is None
    updated = client.patch(
        f"/admin/tenants/{t['id']}",
        json={"webhook_url": "https://acme.example", "max_concurrent_runs": 3},
        headers=ADMIN,
    ).json()
    assert updated["webhook_url"] == "https://acme.example"
    assert updated["max_concurrent_runs"] == 3
    assert client.patch("/admin/tenants/nope", json={"max_concurrent_runs": 1}, headers=ADMIN).status_code == 404
    assert client.patch(f"/admin/tenants/{t['id']}", json={"max_concurrent_runs": 0}, headers=ADMIN).status_code == 422


def test_admin_patch_tenant_rejects_invalid_webhook_url(client, admin_mode):
    t = client.post("/admin/tenants", json={"name": "invalid-webhook"}, headers=ADMIN).json()
    resp = client.patch(
        f"/admin/tenants/{t['id']}",
        json={"webhook_url": "https://bad host.example"},
        headers=ADMIN,
    )
    assert resp.status_code == 422
