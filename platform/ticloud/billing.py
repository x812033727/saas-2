"""Usage aggregation and quota enforcement, shared by the API and scheduler.

Spend is computed from the run accounting the platform already tracks
(Run.cost_usd), bucketed by the run's calendar month in UTC — the same
basis the /usage endpoint reports, so what a tenant is billed/capped on
matches what they see. Judge spend is intentionally excluded (it never
lands in Run.cost_usd), keeping agent spend and eval spend separate.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Job, Run, Tenant


def _month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def tenant_job_ids(session: Session, tenant_id: str) -> list[str]:
    return list(session.scalars(select(Job.id).where(Job.tenant_id == tenant_id)))


def month_to_date_cost(
    session: Session, job_ids: list[str], now: datetime | None = None
) -> float:
    """Sum of run cost across job_ids for the current UTC calendar month."""
    if not job_ids:
        return 0.0
    now = now or datetime.now(timezone.utc)
    this_month = _month_key(now)
    total = 0.0
    for run in session.scalars(select(Run).where(Run.job_id.in_(job_ids))):
        anchor = run.started_at or run.scheduled_at
        if anchor is not None and _month_key(anchor) == this_month:
            total += run.cost_usd
    return round(total, 6)


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
