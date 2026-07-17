from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_connect_args = {}
if settings.database_url.startswith("sqlite"):
    # The worker and API may touch the same SQLite file from different threads.
    _connect_args["check_same_thread"] = False

engine = create_engine(settings.database_url, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _ensure_new_columns()


def _ensure_new_columns() -> None:
    """Additive micro-migrations for databases created before a column/index
    existed. create_all never ALTERs existing tables and skips an existing
    table's indexes entirely, so both new nullable columns AND new indexes on
    pre-existing tables must be backfilled here (indexed columns added via
    ALTER otherwise stay unindexed on upgraded deployments)."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    new_columns = {
        "jobs": [
            ("tenant_id", "VARCHAR(32)"),
            ("retry_backoff_s", "INTEGER DEFAULT 0"),
            ("approval_required", "BOOLEAN DEFAULT 0"),
        ],
        "runs": [
            ("cancel_requested", "BOOLEAN DEFAULT 0"),
            ("approval_state", "VARCHAR(20)"),
        ],
        "tenants": [
            ("monthly_budget_usd", "FLOAT"),
            ("plan", "VARCHAR(50) DEFAULT 'free'"),
            ("subscription_status", "VARCHAR(30) DEFAULT 'none'"),
            ("stripe_customer_id", "VARCHAR(64)"),
        ],
    }
    # (index name, table, column list) — names match what create_all emits on
    # a fresh DB, so CREATE INDEX IF NOT EXISTS is a no-op there.
    new_indexes = [
        ("ix_jobs_tenant_id", "jobs", "tenant_id"),
        ("ix_jobs_next_run_at", "jobs", "next_run_at"),
        ("ix_tenants_stripe_customer_id", "tenants", "stripe_customer_id"),
        ("ix_runs_job_scheduled", "runs", "job_id, scheduled_at"),
    ]

    with engine.begin() as conn:
        for table, cols in new_columns.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, coltype in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))
        for name, table, cols in new_indexes:
            if table in tables:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))


def get_session() -> Session:
    return SessionLocal()
