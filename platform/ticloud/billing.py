"""Usage aggregation and quota enforcement, shared by the API and scheduler.

Spend is computed from the run accounting the platform already tracks
(Run.cost_usd), bucketed by the run's calendar month in UTC — the same
basis the /usage endpoint reports, so what a tenant is billed/capped on
matches what they see. Judge spend is intentionally excluded (it never
lands in Run.cost_usd), keeping agent spend and eval spend separate.
"""

from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .models import Job, Run, Tenant


def _month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _month_start(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)


def runs_since_filter(start: datetime):
    """A run counts from `start` by its started_at, falling back to
    scheduled_at when it never started — mirrors the per-run anchor logic,
    expressed on the columns so it filters/aggregates in SQL."""
    return or_(
        Run.started_at >= start,
        and_(Run.started_at.is_(None), Run.scheduled_at >= start),
    )


def tenant_job_ids(session: Session, tenant_id: str) -> list[str]:
    return list(session.scalars(select(Job.id).where(Job.tenant_id == tenant_id)))


def month_to_date_cost(
    session: Session, job_ids: list[str], now: datetime | None = None
) -> float:
    """Sum of run cost across job_ids for the current UTC calendar month.

    Aggregates in SQL with a date filter (backed by the Run(job_id,
    scheduled_at) index), so this stays flat as run history grows — it runs
    on the hot path (every trigger, every scheduler tick, every /usage)."""
    if not job_ids:
        return 0.0
    now = now or datetime.now(timezone.utc)
    total = session.scalar(
        select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(
            Run.job_id.in_(job_ids),
            runs_since_filter(_month_start(now)),
        )
    )
    return round(total or 0.0, 6)


def tenant_over_budget(
    session: Session, tenant: Tenant, now: datetime | None = None
) -> bool:
    """True when the tenant has a cap and this month's spend has reached it.

    Uses >= so a tenant exactly at its cap can't start another run. Spend
    can overshoot within a run (guarded per-run by Job.budget_usd), so this
    is a soft account cap, not a hard mid-run kill."""
    if tenant.monthly_budget_usd is None:
        return False
    spent = month_to_date_cost(session, tenant_job_ids(session, tenant.id), now)
    return spent >= tenant.monthly_budget_usd
