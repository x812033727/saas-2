import pytest

from test_api import create_job
from ticloud.config import settings
from ticloud.models import Alert, Run, RunStatus
from ticloud.scheduler.queue import claim_next_run
from ticloud.scheduler.worker import execute_run


ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def admin_mode(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")


@pytest.fixture
def hosted(monkeypatch, admin_mode):
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name):
    tenant_resp = client.post("/admin/tenants", json={"name": name}, headers=ADMIN)
    assert tenant_resp.status_code == 201, tenant_resp.text
    tenant = tenant_resp.json()
    key_resp = client.post(
        f"/admin/tenants/{tenant['id']}/keys",
        json={"name": "qa-key"},
        headers=ADMIN,
    )
    assert key_resp.status_code == 201, key_resp.text
    key = key_resp.json()
    return tenant, {"Authorization": f"Bearer {key['secret']}"}


def _create_approval_job(client, name="qa-review", headers=None):
    resp = client.post(
        "/jobs",
        json={
            "name": name,
            "engine": "offline",
            "cron": None,
            "approval_required": True,
        },
        headers=headers or {},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _hold_run(session, client, job_id, headers=None):
    resp = client.post(f"/jobs/{job_id}/trigger", headers=headers or {})
    assert resp.status_code == 201, resp.text
    run = resp.json()
    session.expire_all()
    claimed = claim_next_run(session)
    assert claimed is not None
    assert claimed.id == run["id"]

    assert execute_run(claimed.id) == RunStatus.AWAITING_APPROVAL
    session.expire_all()
    held = session.get(Run, run["id"])
    assert held.status == RunStatus.AWAITING_APPROVAL
    assert held.approval_state == "pending"
    return run


@pytest.mark.parametrize("action", ["approve", "reject", "cancel"])
def test_review_actions_ack_the_actual_approval_alert(session, client, action):
    job = create_job(client, name=f"qa-{action}", cron=None, approval_required=True)
    run = _hold_run(session, client, job["id"])

    before = client.get("/alerts", params={"acknowledged": False}).json()
    assert [(alert["kind"], alert["run_id"]) for alert in before] == [
        ("approval_required", run["id"])
    ]

    resp = client.post(f"/runs/{run['id']}/{action}")
    assert resp.status_code == 200, resp.text

    open_alerts = client.get("/alerts", params={"acknowledged": False}).json()
    assert open_alerts == []
    acknowledged = client.get("/alerts", params={"acknowledged": True}).json()
    assert [(alert["kind"], alert["run_id"]) for alert in acknowledged] == [
        ("approval_required", run["id"])
    ]


def test_approval_action_does_not_ack_other_alerts_for_same_run(session, client):
    job = create_job(client, name="qa-other-alert", cron=None, approval_required=True)
    run = _hold_run(session, client, job["id"])
    session.add(
        Alert(
            job_id=job["id"],
            run_id=run["id"],
            kind="run_failed",
            message="independent alert must remain visible",
        )
    )
    session.commit()

    resp = client.post(f"/runs/{run['id']}/approve")
    assert resp.status_code == 200, resp.text
    session.expire_all()

    approval_alert = (
        session.query(Alert)
        .filter_by(run_id=run["id"], kind="approval_required")
        .one()
    )
    unrelated_alert = (
        session.query(Alert)
        .filter_by(run_id=run["id"], kind="run_failed")
        .one()
    )
    assert approval_alert.acknowledged is True
    assert unrelated_alert.acknowledged is False
    assert client.get("/alerts/summary").json() == {"unacknowledged": 1}


def test_tenant_approval_does_not_ack_another_tenants_alert(session, client, hosted):
    _, auth_a = _mint_tenant(client, "qa-team-a")
    _, auth_b = _mint_tenant(client, "qa-team-b")
    job_a = _create_approval_job(client, "qa-a-review", headers=auth_a)
    job_b = _create_approval_job(client, "qa-b-review", headers=auth_b)
    run_a = _hold_run(session, client, job_a["id"], headers=auth_a)
    run_b = _hold_run(session, client, job_b["id"], headers=auth_b)

    assert client.get("/alerts/summary", headers=auth_a).json() == {"unacknowledged": 1}
    assert client.get("/alerts/summary", headers=auth_b).json() == {"unacknowledged": 1}

    resp = client.post(f"/runs/{run_a['id']}/approve", headers=auth_a)
    assert resp.status_code == 200, resp.text

    assert client.get("/alerts/summary", headers=auth_a).json() == {"unacknowledged": 0}
    assert client.get("/alerts/summary", headers=auth_b).json() == {"unacknowledged": 1}
    b_open_alerts = client.get("/alerts", params={"acknowledged": False}, headers=auth_b).json()
    assert [(alert["kind"], alert["run_id"]) for alert in b_open_alerts] == [
        ("approval_required", run_b["id"])
    ]
