from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..db import SessionLocal, init_db
from ..eval.failures import cluster_failures
from ..models import Alert, ApiKey, EvalCase, Job, Lesson, Run, RunStatus, Tenant
from ..scheduler.cron import compute_next_run
from ..scheduler.queue import enqueue_manual
from .auth import generate_key, hash_key, make_current_tenant, require_admin
from .schemas import (
    AlertOut,
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    EvalCaseCreate,
    EvalCaseOut,
    FailureModeOut,
    JobCreate,
    JobOut,
    JobWithLastRun,
    LessonOut,
    PromoteRequest,
    RunDetailOut,
    RunOut,
    RunStatPoint,
    TenantCreate,
    TenantOut,
    UsageOut,
    UsagePoint,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Ti Cloud",
    version=__version__,
    description="Agent-native cron/loop scheduling with quality gates.",
    lifespan=lifespan,
)


def db() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# Resolves to None in single-tenant mode; enforces a tenant key in hosted
# ("required") mode. Every data route below scopes its queries when a tenant
# is present, so cross-tenant reads consistently 404.
current_tenant = make_current_tenant(db)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/ui/")


def _jobs_stmt(tenant: Tenant | None):
    stmt = select(Job).order_by(Job.created_at)
    if tenant is not None:
        stmt = stmt.where(Job.tenant_id == tenant.id)
    return stmt


def _tenant_job_ids(session: Session, tenant: Tenant) -> list[str]:
    return list(session.scalars(select(Job.id).where(Job.tenant_id == tenant.id)))


@app.get("/overview", response_model=list[JobWithLastRun])
def overview(
    session: Session = Depends(db), tenant: Tenant | None = Depends(current_tenant)
) -> list[JobWithLastRun]:
    """All jobs with their most recent run, for the dashboard home view."""
    jobs = session.scalars(_jobs_stmt(tenant)).all()
    out = []
    for job in jobs:
        last = session.scalars(
            select(Run)
            .where(Run.job_id == job.id)
            .order_by(Run.scheduled_at.desc())
            .limit(1)
        ).first()
        item = JobWithLastRun.model_validate(job)
        item.last_run = RunOut.model_validate(last) if last else None
        out.append(item)
    return out


@app.post("/jobs", response_model=JobOut, status_code=201)
def create_job(
    body: JobCreate,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Job:
    # Uniqueness is per tenant (NULL tenant = the single self-host namespace),
    # so one tenant's job names neither block nor leak to another's.
    tenant_id = tenant.id if tenant is not None else None
    if session.scalar(select(Job).where(Job.name == body.name, Job.tenant_id == tenant_id)):
        raise HTTPException(409, f"job named {body.name!r} already exists")
    job = Job(**body.model_dump())
    if tenant is not None:
        job.tenant_id = tenant.id
    job.next_run_at = compute_next_run(job)
    session.add(job)
    session.commit()
    return job


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(
    session: Session = Depends(db), tenant: Tenant | None = Depends(current_tenant)
) -> list[Job]:
    return session.scalars(_jobs_stmt(tenant)).all()


def _get_job(session: Session, job_id: str, tenant: Tenant | None = None) -> Job:
    job = session.get(Job, job_id)
    if job is None or (tenant is not None and job.tenant_id != tenant.id):
        raise HTTPException(404, "job not found")
    return job


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(
    job_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Job:
    return _get_job(session, job_id, tenant)


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(
    job_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> None:
    session.delete(_get_job(session, job_id, tenant))
    session.commit()


@app.post("/jobs/{job_id}/trigger", response_model=RunOut, status_code=201)
def trigger_job(
    job_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Run:
    """Fire a job immediately, outside its schedule."""
    return enqueue_manual(session, _get_job(session, job_id, tenant))


@app.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause_job(
    job_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Job:
    job = _get_job(session, job_id, tenant)
    job.paused = True
    session.commit()
    return job


@app.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume_job(
    job_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Job:
    job = _get_job(session, job_id, tenant)
    job.paused = False
    # Re-anchor the schedule so a long pause doesn't fire a backlog.
    job.next_run_at = compute_next_run(job)
    session.commit()
    return job


@app.get("/jobs/{job_id}/runs", response_model=list[RunOut])
def list_runs(
    job_id: str,
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> list[Run]:
    _get_job(session, job_id, tenant)
    return session.scalars(
        select(Run)
        .where(Run.job_id == job_id)
        .order_by(Run.scheduled_at.desc())
        .limit(limit)
    ).all()


@app.get("/jobs/{job_id}/stats", response_model=list[RunStatPoint])
def job_stats(
    job_id: str,
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> list[RunStatPoint]:
    """Recent runs as trend points (oldest first, ready to plot)."""
    _get_job(session, job_id, tenant)
    runs = session.scalars(
        select(Run)
        .where(Run.job_id == job_id)
        .order_by(Run.scheduled_at.desc())
        .limit(limit)
    ).all()
    points = [
        RunStatPoint(
            run_id=r.id,
            status=r.status.value,
            cost_usd=r.cost_usd,
            duration_s=(
                (r.finished_at - r.started_at).total_seconds()
                if r.started_at and r.finished_at
                else None
            ),
            score=r.score,
            scheduled_at=r.scheduled_at,
        )
        for r in runs
    ]
    return list(reversed(points))


@app.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    acknowledged: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> list[Alert]:
    stmt = select(Alert).order_by(Alert.created_at.desc()).limit(limit)
    if acknowledged is not None:
        stmt = stmt.where(Alert.acknowledged.is_(acknowledged))
    if tenant is not None:
        stmt = stmt.where(Alert.job_id.in_(_tenant_job_ids(session, tenant)))
    return session.scalars(stmt).all()


@app.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(
    alert_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Alert:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "alert not found")
    _get_job(session, alert.job_id, tenant)  # 404s for foreign tenants
    alert.acknowledged = True
    session.commit()
    return alert


@app.get("/jobs/{job_id}/lessons", response_model=list[LessonOut])
def job_lessons(
    job_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> list[Lesson]:
    _get_job(session, job_id, tenant)
    return session.scalars(
        select(Lesson).where(Lesson.job_id == job_id).order_by(Lesson.updated_at.desc()).limit(50)
    ).all()


@app.get("/failure-modes", response_model=list[FailureModeOut])
def failure_modes(
    job_id: str | None = None,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> list[FailureModeOut]:
    """Failed runs clustered into recurring failure modes."""
    if job_id is not None:
        _get_job(session, job_id, tenant)
    scope_ids = _tenant_job_ids(session, tenant) if tenant is not None else None
    return [
        FailureModeOut(
            signature=m.signature,
            summary=m.summary,
            count=m.count,
            job_ids=sorted(m.job_ids),
            first_seen=m.first_seen,
            last_seen=m.last_seen,
            sample_run_ids=m.sample_run_ids,
            latest_run_id=m.latest_run_id,
        )
        for m in cluster_failures(session, job_id=job_id, job_ids=scope_ids)
    ]


@app.post("/failure-modes/promote", response_model=EvalCaseOut, status_code=201)
def promote_failure_mode(
    body: PromoteRequest,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> EvalCase:
    """Turn a failure mode into a regression eval case (the flywheel step)."""
    scope_ids = _tenant_job_ids(session, tenant) if tenant is not None else None
    modes = {
        m.signature: m
        for m in cluster_failures(session, job_id=body.job_id, job_ids=scope_ids)
    }
    mode = modes.get(body.signature)
    if mode is None:
        raise HTTPException(404, "failure mode not found")

    latest = session.get(Run, mode.latest_run_id)
    job = latest.job
    # Namespace by job so the same normalized signature (timeouts, connection
    # errors, ...) hit by different jobs/tenants never collides on name.
    name = f"regression-{job.id[:8]}-{mode.signature}"
    if session.scalar(select(EvalCase).where(EvalCase.name == name)):
        raise HTTPException(409, f"eval case {name!r} already exists")

    case = EvalCase(
        name=name,
        job_id=job.id,
        engine=job.engine,
        payload=job.payload or {},
        min_score=body.min_score,
        source_signature=mode.signature,
    )
    session.add(case)
    session.commit()
    return case


@app.get("/eval-cases", response_model=list[EvalCaseOut])
def list_eval_cases(
    session: Session = Depends(db), tenant: Tenant | None = Depends(current_tenant)
) -> list[EvalCase]:
    stmt = select(EvalCase).order_by(EvalCase.created_at)
    if tenant is not None:
        stmt = stmt.where(EvalCase.job_id.in_(_tenant_job_ids(session, tenant)))
    return session.scalars(stmt).all()


@app.post("/eval-cases", response_model=EvalCaseOut, status_code=201)
def create_eval_case(
    body: EvalCaseCreate,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> EvalCase:
    if tenant is not None:
        # Hosted mode: cases must hang off one of the tenant's jobs — a
        # global (job-less) case would leak into every tenant's eval runs.
        if body.job_id is None:
            raise HTTPException(422, "job_id is required in hosted mode")
        _get_job(session, body.job_id, tenant)
    if session.scalar(select(EvalCase).where(EvalCase.name == body.name)):
        raise HTTPException(409, f"eval case named {body.name!r} already exists")
    case = EvalCase(**body.model_dump())
    session.add(case)
    session.commit()
    return case


@app.delete("/eval-cases/{case_id}", status_code=204)
def delete_eval_case(
    case_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> None:
    case = session.get(EvalCase, case_id)
    if case is None:
        raise HTTPException(404, "eval case not found")
    if tenant is not None:
        if case.job_id is None:
            raise HTTPException(404, "eval case not found")
        _get_job(session, case.job_id, tenant)
    session.delete(case)
    session.commit()


@app.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(
    run_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Run:
    run = session.get(Run, run_id)
    if run is None or (tenant is not None and run.job.tenant_id != tenant.id):
        raise HTTPException(404, "run not found")
    return run


# --- usage metering ---------------------------------------------------------


def _usage_months(session: Session, job_ids: list[str] | None) -> list[UsagePoint]:
    """Aggregate run spend per calendar month (UTC), oldest first.

    Bucketing happens in Python so SQLite and Postgres behave identically
    (no dialect-specific date functions)."""
    stmt = select(Run)
    if job_ids is not None:
        stmt = stmt.where(Run.job_id.in_(job_ids))
    buckets: dict[str, UsagePoint] = {}
    for run in session.scalars(stmt):
        anchor = run.started_at or run.scheduled_at
        if anchor is None:
            continue
        month = f"{anchor.year:04d}-{anchor.month:02d}"
        point = buckets.setdefault(
            month,
            UsagePoint(month=month, runs=0, succeeded=0, cost_usd=0.0, tokens_in=0, tokens_out=0),
        )
        point.runs += 1
        if run.status == RunStatus.SUCCEEDED:
            point.succeeded += 1
        point.cost_usd = round(point.cost_usd + run.cost_usd, 6)
        point.tokens_in += run.tokens_in
        point.tokens_out += run.tokens_out
    return [buckets[m] for m in sorted(buckets)]


@app.get("/usage", response_model=UsageOut)
def usage(
    session: Session = Depends(db), tenant: Tenant | None = Depends(current_tenant)
) -> UsageOut:
    """Monthly run/cost/token usage for the calling tenant (all jobs in
    single-tenant mode). Judge spend is deliberately excluded — it lives in
    score details, separate from agent spend."""
    job_ids = _tenant_job_ids(session, tenant) if tenant is not None else None
    return UsageOut(
        tenant_id=tenant.id if tenant is not None else None,
        months=_usage_months(session, job_ids),
    )


# --- admin surface (tenant + API-key management) ----------------------------

admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)], tags=["admin"])


@admin.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(body: TenantCreate, session: Session = Depends(db)) -> Tenant:
    if session.scalar(select(Tenant).where(Tenant.name == body.name)):
        raise HTTPException(409, f"tenant named {body.name!r} already exists")
    tenant = Tenant(name=body.name)
    session.add(tenant)
    session.commit()
    return tenant


@admin.get("/tenants", response_model=list[TenantOut])
def list_tenants(session: Session = Depends(db)) -> list[Tenant]:
    return session.scalars(select(Tenant).order_by(Tenant.created_at)).all()


def _get_tenant(session: Session, tenant_id: str) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant not found")
    return tenant


@admin.post("/tenants/{tenant_id}/keys", response_model=ApiKeyCreated, status_code=201)
def create_api_key(
    tenant_id: str, body: ApiKeyCreate, session: Session = Depends(db)
) -> ApiKeyCreated:
    tenant = _get_tenant(session, tenant_id)
    secret = generate_key()
    key = ApiKey(
        tenant_id=tenant.id, name=body.name, prefix=secret[:12], key_hash=hash_key(secret)
    )
    session.add(key)
    session.commit()
    return ApiKeyCreated.model_validate(
        {**ApiKeyOut.model_validate(key).model_dump(), "secret": secret}
    )


@admin.get("/tenants/{tenant_id}/keys", response_model=list[ApiKeyOut])
def list_api_keys(tenant_id: str, session: Session = Depends(db)) -> list[ApiKey]:
    _get_tenant(session, tenant_id)
    return session.scalars(
        select(ApiKey).where(ApiKey.tenant_id == tenant_id).order_by(ApiKey.created_at)
    ).all()


@admin.post("/keys/{key_id}/revoke", response_model=ApiKeyOut)
def revoke_api_key(key_id: str, session: Session = Depends(db)) -> ApiKey:
    from datetime import datetime, timezone

    key = session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "api key not found")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        session.commit()
    return key


@admin.get("/usage", response_model=list[UsageOut])
def admin_usage(session: Session = Depends(db)) -> list[UsageOut]:
    """Per-tenant monthly usage across all tenants (billing export view).
    Unowned (single-tenant-mode) jobs are reported under tenant_id=None."""
    out = []
    for tenant in session.scalars(select(Tenant).order_by(Tenant.created_at)):
        job_ids = _tenant_job_ids(session, tenant)
        out.append(UsageOut(tenant_id=tenant.id, months=_usage_months(session, job_ids)))
    unowned = list(session.scalars(select(Job.id).where(Job.tenant_id.is_(None))))
    if unowned:
        out.append(UsageOut(tenant_id=None, months=_usage_months(session, unowned)))
    return out


app.include_router(admin)


# Static dashboard — mounted last so API routes take precedence.
_web_dir = Path(__file__).resolve().parent.parent / "web"
if _web_dir.is_dir():  # pragma: no branch
    app.mount("/ui", StaticFiles(directory=_web_dir, html=True), name="ui")
