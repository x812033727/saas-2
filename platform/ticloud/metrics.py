"""Prometheus metrics + structured logging for operating the platform.

Zero-dependency: the exposition text is rendered by hand (same philosophy
as the deterministic failure clustering — self-host stays dependency-free).
Everything is a snapshot from the DB, so it's correct across multiple
workers without shared in-process counters.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Alert, Job, Run, RunStatus

STALE_RUNNING_GRACE_S = 5


def _age_seconds(now: datetime, then: datetime | None) -> float:
    if then is None:
        return 0.0
    return round(max(0.0, (now - then).total_seconds()), 3)


def _is_stale_running(
    started_at: datetime | None,
    scheduled_at: datetime,
    timeout_s: int,
    now: datetime,
) -> bool:
    anchor = started_at or scheduled_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return (now - anchor).total_seconds() > timeout_s + STALE_RUNNING_GRACE_S


def _stale_running_rows(
    session: Session,
    job_ids: list[str] | None = None,
    now: datetime | None = None,
):
    if job_ids is not None and not job_ids:
        return
    now = now or datetime.now(timezone.utc)
    stmt = (
        select(Run.id, Run.job_id, Run.started_at, Run.scheduled_at, Job.timeout_s)
        .join(Job, Run.job_id == Job.id)
        .where(Run.status == RunStatus.RUNNING)
    )
    if job_ids is not None:
        stmt = stmt.where(Run.job_id.in_(job_ids))
    for run_id, job_id, started_at, scheduled_at, timeout_s in session.execute(stmt):
        if _is_stale_running(started_at, scheduled_at, timeout_s, now):
            yield run_id, job_id


def stale_running_counts_by_job(
    session: Session,
    job_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Running runs older than their job timeout plus a small worker grace."""
    if job_ids is not None and not job_ids:
        return {}
    counts = {job_id: 0 for job_id in job_ids or []}
    for _, job_id in _stale_running_rows(session, job_ids, now):
        counts[job_id] = counts.get(job_id, 0) + 1
    return counts


def stale_running_run_ids(
    session: Session,
    job_ids: list[str] | None = None,
    now: datetime | None = None,
) -> set[str]:
    """IDs for stale running runs, optionally narrowed to specific jobs."""
    return {run_id for run_id, _ in _stale_running_rows(session, job_ids, now)}


def render_metrics(session: Session) -> str:
    """Prometheus text exposition of queue, run, job, and spend state."""
    lines: list[str] = []
    now = datetime.now(timezone.utc)

    def metric(name: str, help_text: str, mtype: str, samples: list[tuple[str, float]]):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            suffix = f"{{{labels}}}" if labels else ""
            lines.append(f"{name}{suffix} {value}")

    # Runs by status (queue depth, running, terminal counts).
    by_status = dict(
        session.execute(select(Run.status, func.count(Run.id)).group_by(Run.status)).all()
    )
    metric(
        "ticloud_runs_total",
        "Runs by status.",
        "gauge",
        [(f'status="{s.value}"', by_status.get(s, 0)) for s in RunStatus],
    )

    oldest_queued = session.scalar(
        select(Run).where(Run.status == RunStatus.QUEUED).order_by(Run.scheduled_at).limit(1)
    )
    oldest_running = session.scalar(
        select(Run)
        .where(Run.status == RunStatus.RUNNING)
        .order_by(func.coalesce(Run.started_at, Run.scheduled_at))
        .limit(1)
    )
    metric(
        "ticloud_oldest_queued_run_age_seconds",
        "Age in seconds of the oldest queued run; 0 when the queue is empty.",
        "gauge",
        [("", _age_seconds(now, oldest_queued.scheduled_at if oldest_queued else None))],
    )
    metric(
        "ticloud_oldest_running_run_age_seconds",
        "Age in seconds of the oldest running run; 0 when no run is running.",
        "gauge",
        [
            (
                "",
                _age_seconds(
                    now,
                    (oldest_running.started_at or oldest_running.scheduled_at)
                    if oldest_running
                    else None,
                ),
            )
        ],
    )
    metric(
        "ticloud_stale_running_runs",
        "Running runs older than their job timeout plus a short worker grace period.",
        "gauge",
        [("", sum(stale_running_counts_by_job(session, now=now).values()))],
    )

    # Jobs by paused state.
    paused = session.scalar(select(func.count(Job.id)).where(Job.paused.is_(True))) or 0
    active = session.scalar(select(func.count(Job.id)).where(Job.paused.is_(False))) or 0
    metric(
        "ticloud_jobs",
        "Jobs by scheduling state.",
        "gauge",
        [('state="active"', active), ('state="paused"', paused)],
    )

    # Unacknowledged alerts — the operator's backlog.
    unacked = session.scalar(select(func.count(Alert.id)).where(Alert.acknowledged.is_(False))) or 0
    metric("ticloud_alerts_unacknowledged", "Unacknowledged alerts.", "gauge", [("", unacked)])

    # Cumulative spend and tokens (all-time).
    cost = session.scalar(select(func.coalesce(func.sum(Run.cost_usd), 0.0))) or 0.0
    tin = session.scalar(select(func.coalesce(func.sum(Run.tokens_in), 0))) or 0
    tout = session.scalar(select(func.coalesce(func.sum(Run.tokens_out), 0))) or 0
    metric("ticloud_cost_usd_total", "Cumulative run cost (USD).", "counter", [("", round(cost, 6))])
    metric(
        "ticloud_tokens_total",
        "Cumulative tokens.",
        "counter",
        [('direction="in"', tin), ('direction="out"', tout)],
    )

    return "\n".join(lines) + "\n"


# --- structured logging ------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with any run/job/tenant ids the caller
    attached via ``extra=``. Opt-in so plain-text stays the default."""

    _RESERVED = set(logging.makeLogRecord({}).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(json_logs: bool, level: int = logging.INFO) -> None:
    """Set up root logging: JSON lines when json_logs, else plain text."""
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
