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


def render_metrics(session: Session) -> str:
    """Prometheus text exposition of queue, run, job, and spend state."""
    lines: list[str] = []

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
