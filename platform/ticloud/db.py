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
    """Additive micro-migrations: create_all never ALTERs existing tables,
    so columns added after a deployment are backfilled here (nullable only)."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "jobs" in tables:
        columns = {c["name"] for c in inspector.get_columns("jobs")}
        if "tenant_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN tenant_id VARCHAR(32)"))
    if "tenants" in tables:
        columns = {c["name"] for c in inspector.get_columns("tenants")}
        if "monthly_budget_usd" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE tenants ADD COLUMN monthly_budget_usd FLOAT"))


def get_session() -> Session:
    return SessionLocal()
