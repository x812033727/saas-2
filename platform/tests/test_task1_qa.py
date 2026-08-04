from test_api import create_job
from ticloud.models import Run, RunStatus
from ticloud.scheduler.queue import claim_next_run
from ticloud.scheduler.worker import execute_run


def _hold_for_approval(session, client):
    job = create_job(client, cron=None, approval_required=True)
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == run["id"]
    assert execute_run(claimed.id) == RunStatus.AWAITING_APPROVAL
    session.expire_all()
    assert session.get(Run, run["id"]).status == RunStatus.AWAITING_APPROVAL
    return run


def test_qa_cancel_awaiting_approval_is_terminal_and_not_requeueable(session, client):
    run = _hold_for_approval(session, client)

    resp = client.post(f"/runs/{run['id']}/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["error"] == "cancelled by user"
    assert body["finished_at"] is not None

    session.expire_all()
    stored = session.get(Run, run["id"])
    assert stored.status == RunStatus.CANCELLED
    assert stored.finished_at is not None
    assert claim_next_run(session) is None
    assert client.get("/approvals").json() == []

    assert client.post(f"/runs/{run['id']}/approve").status_code == 409
    assert client.post(f"/runs/{run['id']}/reject").status_code == 409
    assert client.post(f"/runs/{run['id']}/cancel").status_code == 409


def test_qa_job_patch_rejects_unknown_field_without_partial_update(client):
    job = create_job(client, cron=None, budget_usd=1.0, payload={"kept": True})

    resp = client.patch(
        f"/jobs/{job['id']}",
        json={"budget_usd": 9.0, "payload": {"kept": False}, "budget_usdd": 9.0},
    )

    assert resp.status_code == 422
    assert "budget_usdd" in resp.text

    stored = client.get(f"/jobs/{job['id']}").json()
    assert stored["budget_usd"] == 1.0
    assert stored["payload"] == {"kept": True}


def test_qa_job_create_rejects_unknown_top_level_but_allows_payload_keys(client):
    bad = client.post(
        "/jobs",
        json={
            "name": "qa-extra-field",
            "cron": None,
            "payload": {"arbitrary": {"nested": True}},
            "approval_requiredd": True,
        },
    )
    assert bad.status_code == 422
    assert "approval_requiredd" in bad.text
    assert client.get("/jobs").json() == []

    good = client.post(
        "/jobs",
        json={
            "name": "qa-payload-keys",
            "cron": None,
            "payload": {"arbitrary": {"nested": True}},
        },
    )
    assert good.status_code == 201, good.text
    assert good.json()["payload"] == {"arbitrary": {"nested": True}}
