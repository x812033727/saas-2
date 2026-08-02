"""QA regressions for cancelling approval-held runs.

These tests cover the non-happy path around the approval queue: cancellation
must terminally remove a held run from that queue, and the newly exposed UI
action must not weaken tenant isolation.
"""

import pytest

from test_tenancy import ADMIN, _mint_tenant
from ticloud.config import settings
from ticloud.models import Run, RunStatus
from ticloud.scheduler.queue import claim_next_run
from ticloud.scheduler.worker import execute_run


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(
        settings, "admin_token", ADMIN["Authorization"].removeprefix("Bearer ")
    )
    monkeypatch.setattr(settings, "auth_mode", "required")


def _create_approval_held_run(client, session, headers=None):
    headers = headers or {}
    job = client.post(
        "/jobs",
        json={"name": "approval-qa", "cron": None, "approval_required": True},
        headers=headers,
    ).json()
    assert job["id"]

    run = client.post(f"/jobs/{job['id']}/trigger", headers=headers).json()
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == run["id"]

    execute_run(claimed.id)
    session.expire_all()
    assert session.get(Run, run["id"]).status == RunStatus.AWAITING_APPROVAL
    return run


def test_cancelled_approval_run_leaves_queue_and_blocks_late_actions(client, session):
    run = _create_approval_held_run(client, session)
    assert [r["id"] for r in client.get("/approvals").json()] == [run["id"]]

    cancelled = client.post(f"/runs/{run['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["error"] == "cancelled by user"
    assert body["finished_at"] is not None

    assert client.get("/approvals").json() == []
    assert claim_next_run(session) is None
    assert client.post(f"/runs/{run['id']}/approve").status_code == 409
    assert client.post(f"/runs/{run['id']}/reject").status_code == 409
    assert client.post(f"/runs/{run['id']}/cancel").status_code == 409

    session.expire_all()
    stored = session.get(Run, run["id"])
    assert stored.status == RunStatus.CANCELLED
    assert stored.approval_state == "cancelled"
    assert stored.cancel_requested is False


def test_approval_cancel_is_tenant_scoped(client, hosted, session):
    _, _, auth_a = _mint_tenant(client, "team-a")
    _, _, auth_b = _mint_tenant(client, "team-b")
    run = _create_approval_held_run(client, session, headers=auth_a)

    assert client.get("/approvals", headers=auth_b).json() == []
    assert client.post(f"/runs/{run['id']}/cancel").status_code == 401
    assert client.post(f"/runs/{run['id']}/cancel", headers=auth_b).status_code == 404

    session.expire_all()
    assert session.get(Run, run["id"]).status == RunStatus.AWAITING_APPROVAL

    owner_cancel = client.post(f"/runs/{run['id']}/cancel", headers=auth_a)
    assert owner_cancel.status_code == 200, owner_cancel.text
    assert owner_cancel.json()["status"] == "cancelled"
    session.expire_all()
    assert session.get(Run, run["id"]).approval_state == "cancelled"
