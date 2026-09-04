import pytest

from ticloud.config import settings
from ticloud.scheduler.worker import execute_run


ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def _create_job(client, *, name, payload, headers=None):
    resp = client.post(
        "/jobs",
        json={
            "name": name,
            "engine": "offline",
            "cron": None,
            "max_retries": 0,
            "payload": payload,
        },
        headers=headers or {},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _trigger_failures(client, job_id, *, count=1, headers=None):
    for _ in range(count):
        resp = client.post(f"/jobs/{job_id}/trigger", headers=headers or {})
        assert resp.status_code == 201, resp.text
        execute_run(resp.json()["id"])


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN)
    assert tenant.status_code == 201, tenant.text
    key = client.post(
        f"/admin/tenants/{tenant.json()['id']}/keys",
        json={"name": "qa"},
        headers=ADMIN,
    )
    assert key.status_code == 201, key.text
    return {"Authorization": f"Bearer {key.json()['secret']}"}


def test_qa_failure_modes_combines_min_count_and_unpromoted_filters(client):
    repeated = _create_job(
        client,
        name="qa-repeated",
        payload={"fail_at": 2},
    )
    one_off = _create_job(
        client,
        name="qa-one-off",
        payload={"fail_at": 0},
    )
    _trigger_failures(client, repeated["id"], count=2)
    _trigger_failures(client, one_off["id"])

    recurring = client.get("/failure-modes?min_count=2")
    assert recurring.status_code == 200, recurring.text
    assert len(recurring.json()) == 1
    assert recurring.json()[0]["summary"].startswith(
        "RuntimeError: simulated failure at step <n>"
    )
    assert recurring.json()[0]["count"] == 2
    assert recurring.json()[0]["promoted"] is False

    sig = recurring.json()[0]["signature"]
    promoted = client.post("/failure-modes/promote", json={"signature": sig})
    assert promoted.status_code == 201, promoted.text

    filtered = client.get("/failure-modes?min_count=2&unpromoted_only=true")
    assert filtered.status_code == 200, filtered.text
    assert filtered.json() == []

    too_high = client.get("/failure-modes?min_count=3")
    assert too_high.status_code == 200, too_high.text
    assert too_high.json() == []

    invalid = client.get("/failure-modes?min_count=not-a-number")
    assert invalid.status_code == 422


def test_qa_job_scoped_unpromoted_filter_does_not_hide_same_signature(client):
    job_a = _create_job(client, name="qa-shared-a", payload={"fail_at": 3})
    job_b = _create_job(client, name="qa-shared-b", payload={"fail_at": 3})
    _trigger_failures(client, job_a["id"])
    _trigger_failures(client, job_b["id"])

    mode_a = client.get(f"/failure-modes?job_id={job_a['id']}").json()[0]
    mode_b = client.get(f"/failure-modes?job_id={job_b['id']}").json()[0]
    assert mode_a["signature"] == mode_b["signature"]

    promoted = client.post(
        "/failure-modes/promote",
        json={"signature": mode_a["signature"], "job_id": job_a["id"]},
    )
    assert promoted.status_code == 201, promoted.text

    scoped_a = client.get(f"/failure-modes?job_id={job_a['id']}").json()
    scoped_b = client.get(f"/failure-modes?job_id={job_b['id']}").json()
    assert scoped_a[0]["promoted"] is True
    assert scoped_b[0]["promoted"] is False

    hidden_a = client.get(
        f"/failure-modes?job_id={job_a['id']}&unpromoted_only=true"
    )
    visible_b = client.get(
        f"/failure-modes?job_id={job_b['id']}&unpromoted_only=true"
    )
    assert hidden_a.status_code == 200, hidden_a.text
    assert visible_b.status_code == 200, visible_b.text
    assert hidden_a.json() == []
    assert [m["signature"] for m in visible_b.json()] == [mode_b["signature"]]

    missing_job = client.get("/failure-modes?job_id=missing-job")
    assert missing_job.status_code == 404


def test_qa_hosted_unpromoted_filter_is_tenant_scoped(client, hosted):
    auth_a = _mint_tenant(client, "qa-team-a")
    auth_b = _mint_tenant(client, "qa-team-b")
    job_a = _create_job(
        client,
        name="qa-tenant-shared",
        payload={"fail_at": 1},
        headers=auth_a,
    )
    job_b = _create_job(
        client,
        name="qa-tenant-shared",
        payload={"fail_at": 1},
        headers=auth_b,
    )
    _trigger_failures(client, job_a["id"], headers=auth_a)
    _trigger_failures(client, job_b["id"], headers=auth_b)

    sig_a = client.get("/failure-modes", headers=auth_a).json()[0]["signature"]
    sig_b = client.get("/failure-modes", headers=auth_b).json()[0]["signature"]
    assert sig_a == sig_b

    promoted_a = client.post(
        "/failure-modes/promote",
        json={"signature": sig_a},
        headers=auth_a,
    )
    assert promoted_a.status_code == 201, promoted_a.text

    modes_a = client.get("/failure-modes", headers=auth_a).json()
    modes_b = client.get("/failure-modes", headers=auth_b).json()
    assert modes_a[0]["promoted"] is True
    assert modes_b[0]["promoted"] is False

    unpromoted_a = client.get(
        "/failure-modes?unpromoted_only=true",
        headers=auth_a,
    )
    unpromoted_b = client.get(
        "/failure-modes?unpromoted_only=true",
        headers=auth_b,
    )
    assert unpromoted_a.status_code == 200, unpromoted_a.text
    assert unpromoted_b.status_code == 200, unpromoted_b.text
    assert unpromoted_a.json() == []
    assert [m["signature"] for m in unpromoted_b.json()] == [sig_b]
