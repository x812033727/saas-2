from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .validation import validate_webhook_url as _validate_webhook_url


DEFAULT_STRIPE_PLAN_BUDGETS: dict[str, float | None] = {
    "free": 5.0,
    "team": 200.0,
    "pro": None,
}


class Settings(BaseSettings):
    """Platform configuration, overridable via TICLOUD_* env vars."""

    model_config = SettingsConfigDict(env_prefix="TICLOUD_")

    # SQLite by default so `docker-compose` / local dev works with zero deps;
    # production points this at Postgres (postgresql+psycopg2://...).
    database_url: str = "sqlite:///./ticloud.db"

    # Scheduler tick interval (seconds): how often due jobs are enqueued.
    tick_interval: float = 5.0

    # Worker poll interval (seconds) when the queue is empty.
    poll_interval: float = 2.0

    # Default per-run guards, overridable per job.
    default_timeout_s: int = 1800
    default_budget_usd: float = 5.0
    default_max_retries: int = 2

    # Path to a Ti checkout for the TiEngine adapter (optional).
    ti_path: str | None = None

    # Alert webhook (generic JSON POST, Slack incoming-webhook compatible).
    webhook_url: str | None = Field(default=None, max_length=500)

    # Global cap on simultaneously RUNNING runs across all workers; 0 =
    # unlimited. Per-tenant caps (Tenant.max_concurrent_runs) apply on top.
    max_concurrent_runs: int = 0

    # Emit one-JSON-object-per-line logs (with run/job/tenant ids) instead of
    # plain text — for shipping to a log aggregator.
    log_json: bool = False

    # Multi-tenant hosted mode: "off" (default, single-tenant self-host —
    # no auth, jobs unowned) or "required" (every data route needs a tenant
    # API key and sees only that tenant's jobs/runs/alerts).
    auth_mode: Literal["off", "required"] = "off"
    # Bearer token for the /admin surface (tenant + API-key management,
    # cross-tenant usage). Admin routes are disabled while unset.
    admin_token: str | None = None

    # Stripe billing (optional). Without the webhook secret the webhook
    # endpoint parses events unverified (local/dev only); production must set
    # it so signatures are checked. The secret key is used by the app to
    # create Checkout sessions (not needed to receive webhooks).
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    # JSON env override, e.g.
    # TICLOUD_STRIPE_PLAN_BUDGETS='{"free": 10, "team": 500, "pro": null}'
    # None means unlimited. Unknown Stripe plan names still fall back to free.
    stripe_plan_budgets: dict[str, float | None] = Field(
        default_factory=lambda: DEFAULT_STRIPE_PLAN_BUDGETS.copy()
    )

    @field_validator("auth_mode", mode="before")
    @classmethod
    def normalize_auth_mode(cls, value):
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, value: str | None) -> str | None:
        return _validate_webhook_url(value)

    @field_validator("stripe_plan_budgets")
    @classmethod
    def validate_stripe_plan_budgets(
        cls, value: dict[str, float | None]
    ) -> dict[str, float | None]:
        normalized: dict[str, float | None] = {}
        for plan, budget in value.items():
            plan = plan.strip()
            if not plan:
                raise ValueError("stripe plan names must be non-empty")
            if plan in normalized:
                raise ValueError("stripe plan names must be unique after trimming")
            if budget is not None and budget < 0:
                raise ValueError("stripe plan budgets must be non-negative")
            normalized[plan] = budget
        if "free" not in normalized:
            raise ValueError("stripe_plan_budgets must include a free plan")
        return normalized


settings = Settings()
