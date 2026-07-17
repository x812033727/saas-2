from pydantic_settings import BaseSettings, SettingsConfigDict


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
    webhook_url: str | None = None

    # Multi-tenant hosted mode: "off" (default, single-tenant self-host —
    # no auth, jobs unowned) or "required" (every data route needs a tenant
    # API key and sees only that tenant's jobs/runs/alerts).
    auth_mode: str = "off"
    # Bearer token for the /admin surface (tenant + API-key management,
    # cross-tenant usage). Admin routes are disabled while unset.
    admin_token: str | None = None


settings = Settings()
