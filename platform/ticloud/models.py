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
    UniqueConstraint,
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


class Tenant(Base):
    """A paying customer / team in hosted mode. In the default single-tenant
    self-host mode no tenants exist and jobs stay unowned (tenant_id NULL)."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # Calendar-month spend cap (USD) across the tenant's jobs; None = unlimited.
    # At/over the cap, new runs are refused (manual) or skipped (scheduled).
    # Usually set from the subscription plan, but can be overridden per tenant.
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Billing: the plan drives the default budget; Stripe webhooks keep these
    # in sync. Self-managed comp accounts can also be set directly by admin.
    plan: Mapped[str] = mapped_column(String(50), default="free")
    subscription_status: Mapped[str] = mapped_column(String(30), default="none")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    api_keys: Mapped[list["ApiKey"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    """A bearer credential for a tenant. Only the SHA-256 of the secret is
    stored; the secret itself is shown once at creation."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(200), default="default")
    # First characters of the secret, for identifying a key without the secret.
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


class Job(Base):
    """A scheduled agent job: what to run, when, and under what guards."""

    __tablename__ = "jobs"
    # Names are unique per tenant, not globally — two tenants may both own a
    # "daily-sync". NULL tenant (self-host mode) uniqueness is enforced at the
    # API layer, since SQL treats NULLs as distinct in unique constraints.
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_jobs_tenant_name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    # Owning tenant in hosted mode; NULL in single-tenant self-host mode.
    tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, index=True
    )
    tenant: Mapped[Tenant | None] = relationship()
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

    # Quality gate: every finished run is scored 0..1; scoring below the
    # threshold raises an alert, and on_low_score="pause" also pauses the
    # schedule. threshold=None disables the gate (runs are still scored).
    score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    on_low_score: Mapped[str] = mapped_column(String(20), default="alert")
    # Scorer configuration, e.g. {"judge": {"enabled": true, "model": "..."}}.
    scorers: Mapped[dict] = mapped_column(JSON, default=dict)

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
    scores: Mapped[list["ScoreRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
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


class ScoreRecord(Base):
    """One scorer's verdict on a finished run (Run.score holds the overall)."""

    __tablename__ = "score_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    scorer: Mapped[str] = mapped_column(String(50))
    score: Mapped[float] = mapped_column(Float)
    passed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="scores")


class Lesson(Base):
    """Knowledge flywheel: something a job learned that future runs consult.

    Written by the worker on failures and by engines mid-run; read by
    engines before they start work. Deduped per (job, title) — repeat
    failures refresh the lesson instead of piling up duplicates.
    """

    __tablename__ = "lessons"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class EvalCase(Base):
    """A regression test distilled from a production failure (or hand-made).

    Replayed by the eval CLI: run the engine with this payload, score the
    run, fail CI if the score drops below min_score.
    """

    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    engine: Mapped[str] = mapped_column(String(50), default="offline")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    min_score: Mapped[float] = mapped_column(Float, default=0.9)
    source_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Alert(Base):
    """Something a human should look at: low score, exhausted retries, auto-pause."""

    __tablename__ = "alerts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))  # low_score | run_failed | auto_paused
    message: Mapped[str] = mapped_column(Text)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
