"""DB-backed run queue.

Postgres claims use SELECT ... FOR UPDATE SKIP LOCKED so multiple workers
never grab the same run. SQLite (dev/demo) falls back to a plain
transactional claim, which is safe for a single worker process.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..billing import tenant_over_budget
from ..config import settings
from ..models import Alert, Job, Run, RunStatus
from .cron import compute_next_run

log = logging.getLogger(__name__)

# How many oldest-queued runs to consider when the head-of-line run's tenant
# is at its concurrency cap (so one busy tenant can't block the whole queue).
_CLAIM_CANDIDATES = 25


def running_count(session: Session, tenant_id: str | None = None) -> int:
    """Count RUNNING runs, optionally scoped to one tenant's jobs."""
    stmt = select(func.count(Run.id)).where(Run.status == RunStatus.RUNNING)
    if tenant_id is not None:
        stmt = stmt.where(Run.job_id.in_(select(Job.id).where(Job.tenant_id == tenant_id)))
    return session.scalar(stmt) or 0


def _quota_blocked(session: Session, job: Job, now: datetime) -> bool:
    """A tenant at/over its monthly cap has its scheduled runs skipped."""
    tenant = job.tenant if job.tenant_id else None
    return tenant is not None and tenant_over_budget(session, tenant, now)


def _raise_quota_alert_once(session: Session, job: Job) -> None:
    """One unacknowledged quota alert per job — don't spam every fire."""
    existing = session.scalar(
        select(Alert).where(
            Alert.job_id == job.id,
            Alert.kind == "quota_exceeded",
            Alert.acknowledged.is_(False),
        )
    )
    if existing is None:
        session.add(
            Alert(
                job_id=job.id,
                kind="quota_exceeded",
                message=(
                    f"job '{job.name}' skipped: tenant monthly budget "
                    f"(${job.tenant.monthly_budget_usd:.2f}) reached"
                ),
            )
        )


def enqueue_due_jobs(session: Session, now: datetime | None = None) -> list[Run]:
    """Scheduler tick: create queued runs for every due, unpaused job.

    Advances next_run_at in the same transaction so a crash between
    enqueue and advance can at worst double-fire, never silently skip.
    A job whose tenant is over its monthly budget is skipped (next_run_at
    still advances, so it resumes automatically next period / next month).
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
        job.next_run_at = compute_next_run(job, after=now)
        if _quota_blocked(session, job, now):
            _raise_quota_alert_once(session, job)
            log.info("skipped over-budget job %s, next fire %s", job.name, job.next_run_at)
            continue
        run = Run(job_id=job.id, status=RunStatus.QUEUED, scheduled_at=now)
        session.add(run)
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


def claim_next_run(session: Session, now: datetime | None = None) -> Run | None:
    """Atomically claim one due queued run and mark it RUNNING.

    Respects the global concurrency cap and per-tenant caps: if the oldest
    due run's tenant is at capacity it's skipped and the next eligible run is
    tried, so a burst from one tenant can't monopolise workers or overspend."""
    now = now or datetime.now(timezone.utc)
    if settings.max_concurrent_runs and running_count(session) >= settings.max_concurrent_runs:
        return None

    stmt = (
        select(Run)
        .where(Run.status == RunStatus.QUEUED, Run.scheduled_at <= now)
        .order_by(Run.scheduled_at)
        .limit(_CLAIM_CANDIDATES)
    )
    if session.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    for run in session.scalars(stmt):
        tenant = run.job.tenant
        if (
            tenant is not None
            and tenant.max_concurrent_runs is not None
            and running_count(session, tenant.id) >= tenant.max_concurrent_runs
        ):
            continue  # this tenant is at capacity — try the next queued run
        run.status = RunStatus.RUNNING
        run.started_at = datetime.now(timezone.utc)
        session.commit()
        return run
    return None
