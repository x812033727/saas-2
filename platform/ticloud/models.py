import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-safe datetime for schedule correctness on every backend.

    SQLite drops tzinfo, which would make naive/aware comparisons blow up
    (or silently mis-order). Normalize to naive UTC on the way in and
    re-attach UTC on the way out, so all datetimes in the app are aware UTC.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLED = "cancelled"


# Statuses that count as "finished" (no further transitions).
TERMINAL_STATUSES = {
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.TIMED_OUT,
    RunStatus.BUDGET_EXCEEDED,
    RunStatus.CANCELLED,
}


class Job(Base):
    """A scheduled agent job: what to run, when, and under what guards."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    engine: Mapped[str] = mapped_column(String(50), default="offline")
    # Engine-specific payload (e.g. repo URL, task brief for a Ti workshop).
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # Scheduling: cron expression takes precedence; else interval_seconds;
    # a job with neither is manual-trigger-only.
    cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    # Per-run guards (agent-native reliability semantics).
    timeout_s: Mapped[int] = mapped_column(Integer, default=1800)
    budget_usd: Mapped[float] = mapped_column(Float, default=5.0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    runs: Mapped[list["Run"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Run(Base):
    """One execution of a job (scheduled or manually triggered)."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, values_callable=lambda e: [m.value for m in e]),
        default=RunStatus.QUEUED,
        index=True,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1)

    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)

    # Result summary from the engine (e.g. PR URL, verdict) and error detail.
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Quality gate score (Phase 2 fills this in; nullable until then).
    score: Mapped[float | None] = mapped_column(Float, nullable=True)

    job: Mapped[Job] = relationship(back_populates="runs")
    steps: Mapped[list["RunStep"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunStep.index"
    )


class RunStep(Base):
    """Structured trace of one step inside a run (role turn, tool call, ...)."""

    __tablename__ = "run_steps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    index: Mapped[int] = mapped_column(Integer)

    # e.g. role="pm"/"engineer"/"qa", kind="llm"/"tool"/"phase"
    role: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(30), default="phase")
    name: Mapped[str] = mapped_column(String(200))

    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[Run] = relationship(back_populates="steps")
