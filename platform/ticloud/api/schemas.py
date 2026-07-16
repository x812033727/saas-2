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


class AlertOut(BaseModel):
    id: str
    job_id: str
    run_id: str | None
    kind: str
    message: str
    acknowledged: bool
    created_at: datetime

    model_config = {"from_attributes": True}
