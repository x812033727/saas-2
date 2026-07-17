from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..engine import ENGINES
from ..scheduler.cron import validate_cron


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    engine: str = "offline"
    payload: dict = Field(default_factory=dict)
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=10)
    timeout_s: int = Field(default_factory=lambda: settings.default_timeout_s, ge=1)
    budget_usd: float = Field(default_factory=lambda: settings.default_budget_usd, gt=0)
    max_retries: int = Field(default_factory=lambda: settings.default_max_retries, ge=0)
    retry_backoff_s: int = Field(default=0, ge=0)
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    on_low_score: str = "alert"
    scorers: dict = Field(default_factory=dict)

    @field_validator("on_low_score")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        if v not in ("alert", "pause"):
            raise ValueError("on_low_score must be 'alert' or 'pause'")
        return v

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, v: str | None) -> str | None:
        if v is not None and not validate_cron(v):
            raise ValueError(f"invalid cron expression: {v!r}")
        return v

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, v: str) -> str:
        if v not in ENGINES:
            raise ValueError(f"unknown engine {v!r}; available: {sorted(ENGINES)}")
        return v


class JobUpdate(BaseModel):
    """Partial update — only provided fields change (use model_fields_set).
    Reuses JobCreate's validators; schedule fields re-anchor next_run_at."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    payload: dict | None = None
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=10)
    timeout_s: int | None = Field(default=None, ge=1)
    budget_usd: float | None = Field(default=None, gt=0)
    max_retries: int | None = Field(default=None, ge=0)
    retry_backoff_s: int | None = Field(default=None, ge=0)
    score_threshold: float | None = Field(default=None, ge=0, le=1)
    on_low_score: str | None = None
    scorers: dict | None = None

    @field_validator("on_low_score")
    @classmethod
    def _valid_action(cls, v: str | None) -> str | None:
        if v is not None and v not in ("alert", "pause"):
            raise ValueError("on_low_score must be 'alert' or 'pause'")
        return v

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, v: str | None) -> str | None:
        if v is not None and not validate_cron(v):
            raise ValueError(f"invalid cron expression: {v!r}")
        return v


class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    engine: str
    cron: str | None = None
    interval_seconds: int | None = None
    payload: dict
    required_payload: list[str] = Field(default_factory=list)


class TemplateInstantiate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    cron: str | None = None  # overrides the template schedule
    payload: dict = Field(default_factory=dict)  # merged over the template payload


class JobOut(BaseModel):
    id: str
    name: str
    engine: str
    payload: dict
    cron: str | None
    interval_seconds: int | None
    next_run_at: datetime | None
    paused: bool
    timeout_s: int
    budget_usd: float
    max_retries: int
    retry_backoff_s: int
    score_threshold: float | None
    on_low_score: str
    scorers: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class RunStepOut(BaseModel):
    index: int
    role: str
    kind: str
    name: str
    started_at: datetime
    finished_at: datetime | None
    input: dict | None
    output: dict | None
    cost_usd: float
    tokens_in: int
    tokens_out: int

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    id: str
    job_id: str
    status: str
    attempt: int
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cost_usd: float
    tokens_in: int
    tokens_out: int
    result: dict | None
    error: str | None
    score: float | None

    model_config = {"from_attributes": True}


class ScoreRecordOut(BaseModel):
    scorer: str
    score: float
    passed: bool
    detail: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RunDetailOut(RunOut):
    steps: list[RunStepOut]
    scores: list[ScoreRecordOut]


class JobWithLastRun(JobOut):
    last_run: RunOut | None = None


class RunStatPoint(BaseModel):
    """Lightweight per-run point for trend sparklines (drift view)."""

    run_id: str
    status: str
    cost_usd: float
    duration_s: float | None
    score: float | None
    scheduled_at: datetime


class LessonOut(BaseModel):
    id: str
    job_id: str
    title: str
    content: str
    source_run_id: str | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class FailureModeOut(BaseModel):
    signature: str
    summary: str
    count: int
    job_ids: list[str]
    first_seen: datetime | None
    last_seen: datetime | None
    sample_run_ids: list[str]
    latest_run_id: str | None


class PromoteRequest(BaseModel):
    signature: str
    job_id: str | None = None
    min_score: float = Field(default=0.9, ge=0, le=1)


class EvalCaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    engine: str = "offline"
    payload: dict = Field(default_factory=dict)
    min_score: float = Field(default=0.9, ge=0, le=1)
    job_id: str | None = None

    @field_validator("engine")
    @classmethod
    def _known_engine(cls, v: str) -> str:
        if v not in ENGINES:
            raise ValueError(f"unknown engine {v!r}; available: {sorted(ENGINES)}")
        return v


class EvalCaseOut(BaseModel):
    id: str
    name: str
    job_id: str | None
    engine: str
    payload: dict
    min_score: float
    source_signature: str | None
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertOut(BaseModel):
    id: str
    job_id: str
    run_id: str | None
    kind: str
    message: str
    acknowledged: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    id: str
    name: str
    monthly_budget_usd: float | None
    plan: str
    subscription_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantBudget(BaseModel):
    # None clears the cap (unlimited).
    monthly_budget_usd: float | None = Field(default=None, ge=0)


class TenantPlan(BaseModel):
    plan: str = Field(min_length=1, max_length=50)


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=200)


class ApiKeyOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    prefix: str
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    model_config = {"from_attributes": True}


class ApiKeyCreated(ApiKeyOut):
    # The full secret — returned exactly once, at creation.
    secret: str


class UsagePoint(BaseModel):
    month: str  # "YYYY-MM"
    runs: int
    succeeded: int
    cost_usd: float
    tokens_in: int
    tokens_out: int


class UsageOut(BaseModel):
    tenant_id: str | None
    months: list[UsagePoint]
    monthly_budget_usd: float | None = None
    current_month_cost_usd: float = 0.0
    over_budget: bool = False
