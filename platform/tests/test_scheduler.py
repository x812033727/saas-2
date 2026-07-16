from datetime import datetime, timedelta, timezone

from ticloud.models import Job, Run, RunStatus
from ticloud.scheduler.cron import compute_next_run
from ticloud.scheduler.queue import claim_next_run, enqueue_due_jobs, enqueue_manual


def make_job(session, **kw):
    defaults = dict(name="test-job", engine="offline", payload={})
    defaults.update(kw)
    job = Job(**defaults)
    session.add(job)
    session.commit()
    return job


def test_cron_next_run():
    job = Job(name="j", cron="0 2 * * *")  # daily at 02:00 UTC
    after = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)
    nxt = compute_next_run(job, after=after)
    assert (nxt.hour, nxt.minute, nxt.day) == (2, 0, 17)


def test_interval_next_run():
    job = Job(name="j", interval_seconds=900)
    after = datetime(2026, 7, 16, tzinfo=timezone.utc)
    assert compute_next_run(job, after=after) == after + timedelta(seconds=900)


def test_manual_only_job_has_no_next_run():
    assert compute_next_run(Job(name="j")) is None


def test_enqueue_due_jobs_advances_schedule(session):
    now = datetime.now(timezone.utc)
    job = make_job(session, interval_seconds=3600, next_run_at=now - timedelta(minutes=1))
    created = enqueue_due_jobs(session, now=now)
    assert len(created) == 1

    session.refresh(job)
    assert job.next_run_at > now  # advanced, so no immediate double fire
    assert enqueue_due_jobs(session, now=now) == []


def test_paused_job_not_enqueued(session):
    now = datetime.now(timezone.utc)
    make_job(session, interval_seconds=60, next_run_at=now - timedelta(minutes=1), paused=True)
    assert enqueue_due_jobs(session, now=now) == []


def test_claim_marks_running_and_drains(session):
    job = make_job(session)
    enqueue_manual(session, job)

    run = claim_next_run(session)
    assert run is not None and run.status == RunStatus.RUNNING
    assert run.started_at is not None
    assert claim_next_run(session) is None  # queue drained


def test_claim_oldest_first(session):
    job = make_job(session)
    now = datetime.now(timezone.utc)
    newer = Run(job_id=job.id, scheduled_at=now)
    older = Run(job_id=job.id, scheduled_at=now - timedelta(minutes=5))
    session.add_all([newer, older])
    session.commit()

    assert claim_next_run(session).id == older.id
