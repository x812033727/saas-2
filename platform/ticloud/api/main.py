import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pydantic import ValidationError as PydanticValidationError

from .. import stripe_billing, templates
from .. import __version__
from ..billing import month_to_date_cost, runs_since_filter, tenant_over_budget
from ..db import SessionLocal, init_db
from ..eval.failures import cluster_failures
from ..config import settings
from ..metrics import configure_logging, render_metrics
from ..models import (
    TERMINAL_STATUSES,
    Alert,
    ApiKey,
    EvalCase,
    Job,
    Lesson,
    Run,
    RunStatus,
    RunStep,
    Tenant,
)
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
    JobUpdate,
    JobWithLastRun,
    LessonOut,
    PromoteRequest,
    RunDetailOut,
    RunOut,
    RunStatPoint,
    TemplateInstantiate,
    TemplateOut,
    TenantBudget,
    TenantCreate,
    TenantOut,
    TenantPlan,
    TenantUpdate,
    UsageOut,
    UsagePoint,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.log_json:
        configure_logging(True)
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
log = logging.getLogger(__name__)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics(session: Session = Depends(db)) -> PlainTextResponse:
    """Prometheus exposition: queue depth, runs by status, jobs, unacked
    alerts, cumulative spend/tokens. Unauthenticated (aggregate counts only,
    no per-tenant data) so a scraper can reach it."""
    return PlainTextResponse(render_metrics(session))


@app.post("/billing/stripe/webhook", include_in_schema=False)
async def stripe_webhook(request: Request, session: Session = Depends(db)) -> dict:
    """Stripe subscription webhook: syncs each tenant's plan → spend cap.

    No API key — Stripe authenticates via the signature header (verified when
    TICLOUD_STRIPE_WEBHOOK_SECRET is set). Always 200s on well-formed events
    so Stripe doesn't retry a delivered-but-ignored event forever."""
    payload = await request.body()
    try:
        event = stripe_billing.parse_event(payload, request.headers.get("stripe-signature"))
    except stripe_billing.SignatureError:
        raise HTTPException(400, "invalid stripe signature")
    except ValueError:
        raise HTTPException(400, "malformed webhook payload")
    result = stripe_billing.handle_event(session, event)
    log.info("stripe webhook %s -> %s", event.get("type"), result)
    return {"result": result}


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


def _latest_run_by_job(session: Session, job_ids: list[str]) -> dict[str, Run]:
    """Most recent run per job in one query (avoids a per-job N+1).

    A window function ranks each job's runs by scheduled_at (id breaks
    ties); SQLite 3.25+ and Postgres both support it."""
    if not job_ids:
        return {}
    rn = func.row_number().over(
        partition_by=Run.job_id,
        order_by=(Run.scheduled_at.desc(), Run.id.desc()),
    ).label("rn")
    ranked = select(Run.id, rn).where(Run.job_id.in_(job_ids)).subquery()
    latest_ids = select(ranked.c.id).where(ranked.c.rn == 1)
    runs = session.scalars(select(Run).where(Run.id.in_(latest_ids)))
    return {r.job_id: r for r in runs}


@app.get("/overview", response_model=list[JobWithLastRun])
def overview(
    session: Session = Depends(db), tenant: Tenant | None = Depends(current_tenant)
) -> list[JobWithLastRun]:
    """All jobs with their most recent run, for the dashboard home view."""
    jobs = session.scalars(_jobs_stmt(tenant)).all()
    latest = _latest_run_by_job(session, [j.id for j in jobs])
    out = []
    for job in jobs:
        item = JobWithLastRun.model_validate(job)
        last = latest.get(job.id)
        item.last_run = RunOut.model_validate(last) if last else None
        out.append(item)
    return out


def _persist_new_job(session: Session, tenant: Tenant | None, body: JobCreate) -> Job:
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


@app.post("/jobs", response_model=JobOut, status_code=201)
def create_job(
    body: JobCreate,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Job:
    return _persist_new_job(session, tenant, body)


@app.get("/templates", response_model=list[TemplateOut])
def list_templates() -> list[dict]:
    """Flagship job presets — the one-call path to a repo patrol / dependency
    upgrade / CI babysitter (or an offline demo). Not tenant-scoped."""
    return templates.TEMPLATES


@app.post("/jobs/from-template/{template_id}", response_model=JobOut, status_code=201)
def create_from_template(
    template_id: str,
    body: TemplateInstantiate,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Job:
    """Create a job from a template, merging in the caller's name, optional
    cron override, and payload (e.g. the repo URL)."""
    template = templates.get_template(template_id)
    if template is None:
        raise HTTPException(404, "template not found")
    fields = templates.build_job_fields(template, body.name, body.cron, body.payload)
    missing = templates.missing_required(template, fields["payload"])
    if missing:
        raise HTTPException(422, f"template {template_id!r} requires payload keys: {missing}")
    try:
        job_create = JobCreate(**fields)  # reuse engine/cron/action validation
    except PydanticValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    return _persist_new_job(session, tenant, job_create)


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


_SCHEDULE_FIELDS = {"cron", "interval_seconds"}


@app.patch("/jobs/{job_id}", response_model=JobOut)
def update_job(
    job_id: str,
    body: JobUpdate,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Job:
    """Partial update — change schedule/guards/payload without losing the
    job's run history, lessons, and failure clusters (delete+recreate did)."""
    job = _get_job(session, job_id, tenant)
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] != job.name:
        tenant_id = tenant.id if tenant is not None else None
        clash = session.scalar(
            select(Job).where(
                Job.name == changes["name"], Job.tenant_id == tenant_id, Job.id != job.id
            )
        )
        if clash is not None:
            raise HTTPException(409, f"job named {changes['name']!r} already exists")
    for field, value in changes.items():
        setattr(job, field, value)
    if _SCHEDULE_FIELDS & changes.keys():
        job.next_run_at = compute_next_run(job)
    session.commit()
    return job


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
    job = _get_job(session, job_id, tenant)
    if tenant is not None and tenant_over_budget(session, tenant):
        raise HTTPException(
            402, f"tenant monthly budget (${tenant.monthly_budget_usd:.2f}) reached"
        )
    return enqueue_manual(session, job)


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


def _keyset_before(ts_col, id_col, cursor: str | None):
    """Keyset filter for descending (ts, id) pages. `cursor` is the last seen
    item's "<ts_iso>|<id>"; returns rows strictly older. Bad cursor → 422."""
    if not cursor:
        return None
    try:
        ts_str, _, cid = cursor.rpartition("|")
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        raise HTTPException(422, "invalid cursor")
    from sqlalchemy import and_, or_

    return or_(ts_col < ts, and_(ts_col == ts, id_col < cid))


@app.get("/jobs/{job_id}/runs", response_model=list[RunOut])
def list_runs(
    job_id: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> list[Run]:
    """Newest first. Paginate with `cursor` = the last row's
    "<scheduled_at>|<id>" (keyset, so new rows don't shift pages)."""
    _get_job(session, job_id, tenant)
    stmt = select(Run).where(Run.job_id == job_id)
    keyset = _keyset_before(Run.scheduled_at, Run.id, cursor)
    if keyset is not None:
        stmt = stmt.where(keyset)
    return session.scalars(
        stmt.order_by(Run.scheduled_at.desc(), Run.id.desc()).limit(limit)
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
    # Step counts for the whole page in one grouped query (no per-run N+1).
    run_ids = [r.id for r in runs]
    step_counts = dict(
        session.execute(
            select(RunStep.run_id, func.count(RunStep.id))
            .where(RunStep.run_id.in_(run_ids))
            .group_by(RunStep.run_id)
        ).all()
    ) if run_ids else {}
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
            steps=step_counts.get(r.id, 0),
            scheduled_at=r.scheduled_at,
        )
        for r in runs
    ]
    return list(reversed(points))


@app.get("/alerts", response_model=list[AlertOut])
def list_alerts(
    acknowledged: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    cursor: str | None = None,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> list[Alert]:
    """Newest first. Paginate with `cursor` = the last row's
    "<created_at>|<id>"."""
    stmt = select(Alert)
    if acknowledged is not None:
        stmt = stmt.where(Alert.acknowledged.is_(acknowledged))
    if tenant is not None:
        stmt = stmt.where(Alert.job_id.in_(_tenant_job_ids(session, tenant)))
    keyset = _keyset_before(Alert.created_at, Alert.id, cursor)
    if keyset is not None:
        stmt = stmt.where(keyset)
    return session.scalars(
        stmt.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit)
    ).all()


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


def _get_run(session: Session, run_id: str, tenant: Tenant | None) -> Run:
    run = session.get(Run, run_id)
    if run is None or (tenant is not None and run.job.tenant_id != tenant.id):
        raise HTTPException(404, "run not found")
    return run


@app.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(
    run_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Run:
    return _get_run(session, run_id, tenant)


def _step_event(step: RunStep) -> str:
    data = {
        "index": step.index,
        "role": step.role,
        "kind": step.kind,
        "name": step.name,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "finished_at": step.finished_at.isoformat() if step.finished_at else None,
        "input": step.input,
        "output": step.output,
        "cost_usd": step.cost_usd,
        "tokens_in": step.tokens_in,
        "tokens_out": step.tokens_out,
    }
    return f"event: step\ndata: {json.dumps(data, default=str)}\n\n"


# Safety cap so a stuck-RUNNING run can't hold an SSE connection forever.
_SSE_MAX_POLLS = 3600


@app.get("/runs/{run_id}/events", include_in_schema=False)
def run_events(
    run_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> StreamingResponse:
    """Server-Sent Events: stream a run's trace steps as they're written, so
    the UI shows the workshop grow live without polling. Emits already-written
    steps immediately, then tails new ones until the run reaches a terminal
    status (a final `done` event), then closes."""
    _get_run(session, run_id, tenant)  # authz + existence up front

    async def gen():
        last_index = -1
        for _ in range(_SSE_MAX_POLLS):
            s = SessionLocal()  # fresh session each poll to see the worker's commits
            try:
                run = s.get(Run, run_id)
                if run is None:
                    return
                for step in s.scalars(
                    select(RunStep)
                    .where(RunStep.run_id == run_id, RunStep.index > last_index)
                    .order_by(RunStep.index)
                ):
                    last_index = step.index
                    yield _step_event(step)
                if run.status in TERMINAL_STATUSES:
                    yield f"event: done\ndata: {json.dumps({'status': run.status.value})}\n\n"
                    return
            finally:
                s.close()
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(
    run_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Run:
    """Cancel a queued or running run. A queued run is cancelled immediately;
    a running one is flagged and the worker stops it cooperatively (the
    schedule pause only affects future runs, not an in-flight one)."""
    run = _get_run(session, run_id, tenant)
    if run.status in TERMINAL_STATUSES:
        raise HTTPException(409, f"run already {run.status.value}")
    if run.status == RunStatus.QUEUED:
        run.status = RunStatus.CANCELLED
        run.finished_at = datetime.now(timezone.utc)
    else:  # RUNNING — the worker polls this flag and winds the engine down
        run.cancel_requested = True
    session.commit()
    return run


@app.get("/approvals", response_model=list[RunOut])
def list_approvals(
    session: Session = Depends(db), tenant: Tenant | None = Depends(current_tenant)
) -> list[Run]:
    """Runs held for human approval (the approvals queue)."""
    stmt = select(Run).where(Run.status == RunStatus.AWAITING_APPROVAL)
    if tenant is not None:
        stmt = stmt.where(Run.job_id.in_(_tenant_job_ids(session, tenant)))
    return session.scalars(stmt.order_by(Run.scheduled_at.desc())).all()


@app.post("/runs/{run_id}/approve", response_model=RunOut)
def approve_run(
    run_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Run:
    """Approve a held run: it re-enters the queue and executes."""
    run = _get_run(session, run_id, tenant)
    if run.status != RunStatus.AWAITING_APPROVAL:
        raise HTTPException(409, "run is not awaiting approval")
    run.approval_state = "approved"
    run.status = RunStatus.QUEUED
    run.scheduled_at = datetime.now(timezone.utc)  # claimable immediately
    session.commit()
    return run


@app.post("/runs/{run_id}/reject", response_model=RunOut)
def reject_run(
    run_id: str,
    session: Session = Depends(db),
    tenant: Tenant | None = Depends(current_tenant),
) -> Run:
    """Reject a held run: it terminates without ever executing."""
    run = _get_run(session, run_id, tenant)
    if run.status != RunStatus.AWAITING_APPROVAL:
        raise HTTPException(409, "run is not awaiting approval")
    run.approval_state = "rejected"
    run.status = RunStatus.CANCELLED
    run.error = "rejected by reviewer"
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run


# --- usage metering ---------------------------------------------------------


USAGE_WINDOW_MONTHS = 12


def _window_start(now: datetime, months: int) -> datetime:
    """First of the month `months-1` months back (inclusive window)."""
    year, month = now.year, now.month - (months - 1)
    while month <= 0:
        month += 12
        year -= 1
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _usage_months(session: Session, job_ids: list[str] | None) -> list[UsagePoint]:
    """Aggregate run spend per calendar month (UTC), oldest first, over the
    last USAGE_WINDOW_MONTHS months.

    Bucketing happens in Python so SQLite and Postgres behave identically
    (no dialect-specific date functions), but a SQL window filter bounds the
    scan so it doesn't grow with total run history."""
    start = _window_start(datetime.now(timezone.utc), USAGE_WINDOW_MONTHS)
    stmt = select(Run).where(runs_since_filter(start))
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
    budget = tenant.monthly_budget_usd if tenant is not None else None
    return UsageOut(
        tenant_id=tenant.id if tenant is not None else None,
        months=_usage_months(session, job_ids),
        monthly_budget_usd=budget,
        current_month_cost_usd=month_to_date_cost(session, job_ids),
        over_budget=tenant_over_budget(session, tenant) if tenant is not None else False,
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


@admin.patch("/tenants/{tenant_id}", response_model=TenantOut)
def update_tenant(
    tenant_id: str, body: TenantUpdate, session: Session = Depends(db)
) -> Tenant:
    """Update a tenant's operational settings (alert webhook, concurrency cap)."""
    tenant = _get_tenant(session, tenant_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(tenant, field, value)
    session.commit()
    return tenant


@admin.put("/tenants/{tenant_id}/budget", response_model=TenantOut)
def set_tenant_budget(
    tenant_id: str, body: TenantBudget, session: Session = Depends(db)
) -> Tenant:
    """Set (or clear, with null) a tenant's monthly spend cap.

    Stripe webhooks also set this from the plan; a manual override here wins
    until the next subscription event re-derives it."""
    tenant = _get_tenant(session, tenant_id)
    tenant.monthly_budget_usd = body.monthly_budget_usd
    session.commit()
    return tenant


@admin.put("/tenants/{tenant_id}/plan", response_model=TenantOut)
def set_tenant_plan(
    tenant_id: str, body: TenantPlan, session: Session = Depends(db)
) -> Tenant:
    """Manually set a tenant's plan (comp accounts / when not using Stripe).

    Applies the plan's budget, same as a subscription event would."""
    if body.plan not in stripe_billing.PLAN_BUDGETS:
        raise HTTPException(
            422, f"unknown plan {body.plan!r}; known: {sorted(stripe_billing.PLAN_BUDGETS)}"
        )
    tenant = _get_tenant(session, tenant_id)
    stripe_billing.apply_plan(tenant, body.plan, "active")
    session.commit()
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
