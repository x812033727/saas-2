"""Wave 3 A2 — human-approval gate (pre-execution).

An approval_required job never runs the engine until a human approves the
run; reject terminates it without running.
"""

from test_api import create_job
from ticloud.models import TERMINAL_STATUSES, Run, RunStatus
from ticloud.scheduler.queue import claim_next_run
from ticloud.scheduler.worker import execute_run


def _trigger_and_execute(session, client, job_id):
    run = client.post(f"/jobs/{job_id}/trigger").json()
    execute_run(claim_next_run(session).id)
    session.expire_all()
    return run


def test_gate_holds_run_then_approve_runs_it(session, client):
    job = create_job(client, cron=None, approval_required=True)
    run = _trigger_and_execute(session, client, job["id"])

    held = session.get(Run, run["id"])
    assert held.status == RunStatus.AWAITING_APPROVAL
    assert held.approval_state == "pending"
    assert claim_next_run(session) is None  # not runnable while held

    assert any(x["id"] == run["id"] for x in client.get("/approvals").json())
    assert any(a["kind"] == "approval_required" for a in client.get("/alerts").json())

    approved = client.post(f"/runs/{run['id']}/approve").json()
    assert approved["status"] == "queued"
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == run["id"]
    execute_run(claimed.id)
    session.expire_all()
    assert session.get(Run, run["id"]).status == RunStatus.SUCCEEDED


def test_reject_terminates_without_running(session, client):
    job = create_job(client, cron=None, approval_required=True)
    run = _trigger_and_execute(session, client, job["id"])

    rejected = client.post(f"/runs/{run['id']}/reject").json()
    assert rejected["status"] == "cancelled"
    rr = session.get(Run, run["id"])
    assert rr.approval_state == "rejected" and rr.error == "rejected by reviewer"
    assert claim_next_run(session) is None  # never runs


def test_approve_reject_require_awaiting_state(session, client):
    job = create_job(client, cron=None)  # no gate
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    assert client.post(f"/runs/{run['id']}/approve").status_code == 409
    assert client.post(f"/runs/{run['id']}/reject").status_code == 409


def test_non_approval_job_runs_normally(session, client):
    job = create_job(client, cron=None)
    run = _trigger_and_execute(session, client, job["id"])
    assert session.get(Run, run["id"]).status == RunStatus.SUCCEEDED
    assert client.get("/approvals").json() == []


def test_approval_required_settable_on_create_and_patch(client):
    job = create_job(client, approval_required=True)
    assert job["approval_required"] is True
    plain = create_job(client, name="plain")
    assert plain["approval_required"] is False
    patched = client.patch(f"/jobs/{plain['id']}", json={"approval_required": True}).json()
    assert patched["approval_required"] is True


def test_awaiting_approval_is_not_terminal():
    assert RunStatus.AWAITING_APPROVAL not in TERMINAL_STATUSES
