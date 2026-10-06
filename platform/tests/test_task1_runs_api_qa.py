from datetime import datetime, timedelta, timezone

from ticloud.config import settings
from ticloud.models import Run, RunStatus


ADMIN = {"Authorization": "Bearer admin-secret"}


def _create_job(client, *, name: str, timeout_s: int = 1, headers: dict | None = None):
    resp = client.post(
        "/jobs",
        json={"name": name, "cron": None, "timeout_s": timeout_s},
        headers=headers or {},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _mint_tenant(client, name: str):
    tenant_resp = client.post("/admin/tenants", json={"name": name}, headers=ADMIN)
    assert tenant_resp.status_code == 201, tenant_resp.text
    tenant = tenant_resp.json()
    key_resp = client.post(
        f"/admin/tenants/{tenant['id']}/keys",
        json={"name": "qa"},
        headers=ADMIN,
    )
    assert key_resp.status_code == 201, key_resp.text
    key = key_resp.json()
    return {"Authorization": f"Bearer {key['secret']}"}


def test_qa_runs_summary_static_route_returns_zero_shape_on_empty_db(client):
    resp = client.get("/runs/summary")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "total": 0,
        "by_status": {status.value: 0 for status in RunStatus},
        "stale_running": 0,
    }


def test_qa_global_runs_stale_filter_paginates_and_excludes_non_running(
    client, session
):
    job = _create_job(client, name="qa-runs-paged", timeout_s=1)
    other = _create_job(client, name="qa-runs-other", timeout_s=1)
    now = datetime.now(timezone.utc)
    stale_old = Run(
        job_id=job["id"],
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=40),
        started_at=now - timedelta(seconds=40),
    )
    stale_new = Run(
        job_id=job["id"],
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=20),
        started_at=now - timedelta(seconds=20),
    )
    fresh_running = Run(
        job_id=job["id"],
        status=RunStatus.RUNNING,
        scheduled_at=now,
        started_at=now,
    )
    old_failed = Run(
        job_id=job["id"],
        status=RunStatus.FAILED,
        scheduled_at=now - timedelta(seconds=30),
        finished_at=now - timedelta(seconds=29),
    )
    other_stale = Run(
        job_id=other["id"],
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=10),
        started_at=now - timedelta(seconds=10),
    )
    session.add_all([stale_old, stale_new, fresh_running, old_failed, other_stale])
    session.commit()

    page1_resp = client.get(
        "/runs",
        params={"job_id": job["id"], "stale": "true", "limit": 1},
    )
    assert page1_resp.status_code == 200, page1_resp.text
    page1 = page1_resp.json()
    assert [run["id"] for run in page1] == [stale_new.id]

    cursor = f"{page1[-1]['scheduled_at']}|{page1[-1]['id']}"
    page2_resp = client.get(
        "/runs",
        params={"job_id": job["id"], "stale": "true", "cursor": cursor},
    )
    assert page2_resp.status_code == 200, page2_resp.text
    assert [run["id"] for run in page2_resp.json()] == [stale_old.id]

    bad_cursor_resp = client.get("/runs", params={"cursor": "not-a-keyset"})
    assert bad_cursor_resp.status_code == 422


def test_qa_runs_summary_stale_count_is_tenant_scoped(client, session, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")
    auth_a = _mint_tenant(client, "qa-team-a")
    auth_b = _mint_tenant(client, "qa-team-b")

    job_a = _create_job(client, name="qa-owned-a", timeout_s=1, headers=auth_a)
    job_b = _create_job(client, name="qa-owned-b", timeout_s=1, headers=auth_b)
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            Run(
                job_id=job_a["id"],
                status=RunStatus.RUNNING,
                scheduled_at=now - timedelta(seconds=20),
                started_at=now - timedelta(seconds=20),
            ),
            Run(
                job_id=job_b["id"],
                status=RunStatus.RUNNING,
                scheduled_at=now - timedelta(seconds=20),
                started_at=now - timedelta(seconds=20),
            ),
            Run(
                job_id=job_b["id"],
                status=RunStatus.FAILED,
                scheduled_at=now - timedelta(seconds=10),
                finished_at=now - timedelta(seconds=9),
            ),
        ]
    )
    session.commit()

    summary_a = client.get("/runs/summary", headers=auth_a)
    summary_b = client.get("/runs/summary", headers=auth_b)
    assert summary_a.status_code == 200, summary_a.text
    assert summary_b.status_code == 200, summary_b.text
    assert summary_a.json()["total"] == 1
    assert summary_a.json()["stale_running"] == 1
    assert summary_b.json()["total"] == 2
    assert summary_b.json()["stale_running"] == 1

    cross_tenant = client.get(
        "/runs/summary",
        params={"job_id": job_b["id"]},
        headers=auth_a,
    )
    assert cross_tenant.status_code == 404
