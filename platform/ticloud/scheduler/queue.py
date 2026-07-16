"""DB-backed run queue.

Postgres claims use SELECT ... FOR UPDATE SKIP LOCKED so multiple workers
never grab the same run. SQLite (dev/demo) falls back to a plain
transactional claim, which is safe for a single worker process.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, Run, RunStatus
from .cron import compute_next_run

log = logging.getLogger(__name__)


def enqueue_due_jobs(session: Session, now: datetime | None = None) -> list[Run]:
    """Scheduler tick: create queued runs for every due, unpaused job.

    Advances next_run_at in the same transaction so a crash between
    enqueue and advance can at worst double-fire, never silently skip.
    """
    now = now or datetime.now(timezone.utc)
    due = session.scalars(
        select(Job).where(
            Job.paused.is_(False),
            Job.next_run_at.isnot(None),
            Job.next_run_at <= now,
        )
    ).all()

    created: list[Run] = []
    for job in due:
        run = Run(job_id=job.id, status=RunStatus.QUEUED, scheduled_at=now)
        session.add(run)
        job.next_run_at = compute_next_run(job, after=now)
        created.append(run)
        log.info("enqueued run for job %s, next fire %s", job.name, job.next_run_at)
    session.commit()
    return created


def enqueue_manual(session: Session, job: Job) -> Run:
    """Manually trigger a job outside its schedule."""
    run = Run(job_id=job.id, status=RunStatus.QUEUED)
    session.add(run)
    session.commit()
    return run


def claim_next_run(session: Session) -> Run | None:
    """Atomically claim one queued run and mark it RUNNING."""
    stmt = (
        select(Run)
        .where(Run.status == RunStatus.QUEUED)
        .order_by(Run.scheduled_at)
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    run = session.scalars(stmt).first()
    if run is None:
        return None
    run.status = RunStatus.RUNNING
    run.started_at = datetime.now(timezone.utc)
    session.commit()
    return run
