from datetime import datetime, timedelta, timezone

from croniter import croniter

from ..models import Job


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_next_run(job: Job, after: datetime | None = None) -> datetime | None:
    """Next fire time for a job, or None for manual-trigger-only jobs.

    Cron takes precedence over interval. All times are UTC.
    """
    base = _as_utc(after or datetime.now(timezone.utc))
    if job.cron:
        return croniter(job.cron, base).get_next(datetime)
    if job.interval_seconds:
        return base + timedelta(seconds=job.interval_seconds)
    return None


def validate_cron(expr: str) -> bool:
    return croniter.is_valid(expr)
