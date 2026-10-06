import pytest

from test_api import create_job
from ticloud.models import Job, Run, RunStatus
from ticloud.scheduler.queue import ActiveRunError, claim_next_run, enqueue_manual
from ticloud.scheduler.worker import execute_run


def _make_db_job(session, name="qa-overlap"):
    job = Job(name=name, engine="offline", payload={})
    session.add(job)
    session.commit()
    return job


def test_qa_trigger_blocks_same_job_only_and_releases_after_cancel(client):
    job = create_job(client, cron=None)
    other_job = create_job(client, name="independent-job", cron=None)

    first = client.post(f"/jobs/{job['id']}/trigger")
    assert first.status_code == 201, first.text

    same_job = client.post(f"/jobs/{job['id']}/trigger")
    assert same_job.status_code == 409
    assert "active run" in same_job.json()["detail"]

    other = client.post(f"/jobs/{other_job['id']}/trigger")
    assert other.status_code == 201, other.text

    cancelled = client.post(f"/runs/{first.json()['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    retry = client.post(f"/jobs/{job['id']}/trigger")
    assert retry.status_code == 201, retry.text


def test_qa_trigger_blocks_running_and_approval_requeue_states(session, client):
    running_job = create_job(client, cron=None)
    running_run = client.post(f"/jobs/{running_job['id']}/trigger").json()
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == running_run["id"]

    running_blocked = client.post(f"/jobs/{running_job['id']}/trigger")
    assert running_blocked.status_code == 409

    approval_job = create_job(client, name="needs-review", cron=None, approval_required=True)
    held_run = client.post(f"/jobs/{approval_job['id']}/trigger").json()
    execute_run(held_run["id"])

    held_blocked = client.post(f"/jobs/{approval_job['id']}/trigger")
    assert held_blocked.status_code == 409

    approved = client.post(f"/runs/{held_run['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "queued"

    requeued_blocked = client.post(f"/jobs/{approval_job['id']}/trigger")
    assert requeued_blocked.status_code == 409

    execute_run(held_run["id"])
    after_terminal = client.post(f"/jobs/{approval_job['id']}/trigger")
    assert after_terminal.status_code == 201, after_terminal.text


def test_qa_rerun_cannot_bypass_active_run_guard(client):
    job = create_job(client, cron=None)
    original = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(original["id"])

    first_rerun = client.post(f"/runs/{original['id']}/rerun")
    assert first_rerun.status_code == 201, first_rerun.text

    blocked = client.post(f"/runs/{original['id']}/rerun")
    assert blocked.status_code == 409
    assert "active run" in blocked.json()["detail"]

    cancelled = client.post(f"/runs/{first_rerun.json()['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text

    retry = client.post(f"/runs/{original['id']}/rerun")
    assert retry.status_code == 201, retry.text


def test_qa_enqueue_manual_strict_mode_is_job_scoped(session):
    job = _make_db_job(session)
    other_job = _make_db_job(session, name="qa-independent")
    session.add(Run(job_id=job.id, status=RunStatus.QUEUED))
    session.commit()

    with pytest.raises(ActiveRunError):
        enqueue_manual(session, job, allow_active=False)

    default_overlap = enqueue_manual(session, job)
    assert default_overlap.status == RunStatus.QUEUED

    independent = enqueue_manual(session, other_job, allow_active=False)
    assert independent.status == RunStatus.QUEUED
