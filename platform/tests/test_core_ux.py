"""Wave 2 — core product UX: job editing, run cancel, pagination, backoff."""

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from test_api import create_job
from test_scheduler import make_job
from ticloud.db import get_session
from ticloud.models import Run, RunStatus
from ticloud.scheduler.queue import claim_next_run, enqueue_manual
from ticloud.scheduler.worker import execute_run


# --- B4 job editing ----------------------------------------------------------


def test_patch_updates_fields_and_reanchors_schedule(client):
    job = create_job(client, cron="0 2 * * *")
    old_next = job["next_run_at"]
    r = client.patch(f"/jobs/{job['id']}", json={"cron": "0 5 * * *", "budget_usd": 9.0})
    assert r.status_code == 200
    body = r.json()
    assert body["cron"] == "0 5 * * *"
    assert body["budget_usd"] == 9.0
    assert body["next_run_at"] != old_next  # schedule change re-anchored
    assert body["timeout_s"] == job["timeout_s"]  # untouched field preserved


def test_patch_preserves_run_history(session, client):
    job = create_job(client, cron=None)
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])
    client.patch(f"/jobs/{job['id']}", json={"budget_usd": 3.0})
    runs = client.get(f"/jobs/{job['id']}/runs").json()
    assert len(runs) == 1 and runs[0]["id"] == run["id"]  # not lost


def test_patch_name_conflict_and_bad_cron(client):
    create_job(client, name="a")
    b = create_job(client, name="b")
    assert client.patch(f"/jobs/{b['id']}", json={"name": "a"}).status_code == 409
    assert client.patch(f"/jobs/{b['id']}", json={"name": "b"}).status_code == 200  # own name ok
    assert client.patch(f"/jobs/{b['id']}", json={"cron": "nope"}).status_code == 422


def test_patch_missing_job_404(client):
    assert client.patch("/jobs/nope", json={"budget_usd": 1.0}).status_code == 404


# --- B5 run cancellation -----------------------------------------------------


def test_cancel_queued_run(session, client):
    job = create_job(client, cron=None)
    run = client.post(f"/jobs/{job['id']}/trigger").json()  # QUEUED
    r = client.post(f"/runs/{run['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # A cancelled run is never claimed for execution.
    assert claim_next_run(session) is None


def test_cancel_terminal_run_409(session, client):
    job = create_job(client, cron=None)
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])  # succeeds → terminal
    assert client.post(f"/runs/{run['id']}/cancel").status_code == 409


def test_cancel_running_run_stops_it(session):
    """A running engine is cancelled cross-process via the DB flag."""
    job = make_job(session, payload={"sleep_s": 30, "steps": 8})
    enqueue_manual(session, job)
    run = claim_next_run(session)  # RUNNING

    worker = threading.Thread(target=execute_run, args=(run.id,))
    worker.start()
    time.sleep(0.5)  # let the engine enter a sleeping step

    # Simulate POST /cancel from the API process (separate session).
    s2 = get_session()
    r2 = s2.get(Run, run.id)
    r2.cancel_requested = True
    s2.commit()
    s2.close()

    worker.join(timeout=15)
    assert not worker.is_alive()
    session.expire_all()
    assert session.get(Run, run.id).status == RunStatus.CANCELLED


# --- B8 retry backoff --------------------------------------------------------


def test_retry_backoff_delays_next_attempt(session):
    job = make_job(session, payload={"fail_at": 0}, max_retries=1, retry_backoff_s=60)
    enqueue_manual(session, job)
    run = claim_next_run(session)
    execute_run(run.id)  # fails → schedules retry at now+60s

    # The retry exists but its scheduled_at is in the future → not claimable now.
    assert claim_next_run(session) is None
    retry = session.scalars(
        select(Run).where(Run.job_id == job.id, Run.attempt == 2)
    ).first()
    assert retry is not None and retry.status == RunStatus.QUEUED

    # Claimable once its scheduled time arrives.
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    claimed = claim_next_run(session, now=future)
    assert claimed is not None and claimed.attempt == 2


def test_zero_backoff_retries_immediately(session):
    job = make_job(session, payload={"fail_at": 0}, max_retries=1, retry_backoff_s=0)
    enqueue_manual(session, job)
    execute_run(claim_next_run(session).id)
    assert claim_next_run(session) is not None  # retry ready now


# --- B1 pagination -----------------------------------------------------------


def test_runs_keyset_pagination(session, client):
    job = make_job(session)
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        session.add(
            Run(job_id=job.id, status=RunStatus.SUCCEEDED, scheduled_at=base + timedelta(minutes=i))
        )
    session.commit()

    page1 = client.get(f"/jobs/{job.id}/runs", params={"limit": 2}).json()
    assert len(page1) == 2
    last = page1[-1]
    cur = f"{last['scheduled_at']}|{last['id']}"
    page2 = client.get(f"/jobs/{job.id}/runs", params={"limit": 2, "cursor": cur}).json()

    assert len(page2) == 2
    assert not ({r["id"] for r in page1} & {r["id"] for r in page2})  # no overlap
    assert all(r["scheduled_at"] <= last["scheduled_at"] for r in page2)  # strictly older


def test_bad_cursor_422(session, client):
    job = make_job(session)
    assert client.get(f"/jobs/{job.id}/runs", params={"cursor": "garbage"}).status_code == 422
