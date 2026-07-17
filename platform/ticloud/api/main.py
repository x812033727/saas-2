from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..db import SessionLocal, init_db
from ..eval.failures import cluster_failures
from ..models import Alert, EvalCase, Job, Lesson, Run
from ..scheduler.cron import compute_next_run
from ..scheduler.queue import enqueue_manual
from .schemas import (
    AlertOut,
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/ui/")


@app.get("/overview", response_model=list[JobWithLastRun])
def overview(session: Session = Depends(db)) -> list[JobWithLastRun]:
    """All jobs with their most recent run, for the dashboard home view."""
    jobs = session.scalars(select(Job).order_by(Job.created_at)).all()
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
def create_job(body: JobCreate, session: Session = Depends(db)) -> Job:
    if session.scalar(select(Job).where(Job.name == body.name)):
        raise HTTPException(409, f"job named {body.name!r} already exists")
    job = Job(**body.model_dump())
    job.next_run_at = compute_next_run(job)
    session.add(job)
    session.commit()
    return job


@app.get("/jobs", response_model=list[JobOut])
def list_jobs(session: Session = Depends(db)) -> list[Job]:
    return session.scalars(select(Job).order_by(Job.created_at)).all()


def _get_job(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, session: Session = Depends(db)) -> Job:
    return _get_job(session, job_id)


@app.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, session: Session = Depends(db)) -> None:
    session.delete(_get_job(session, job_id))
    session.commit()


@app.post("/jobs/{job_id}/trigger", response_model=RunOut, status_code=201)
def trigger_job(job_id: str, session: Session = Depends(db)) -> Run:
    """Fire a job immediately, outside its schedule."""
    return enqueue_manual(session, _get_job(session, job_id))


@app.post("/jobs/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: str, session: Session = Depends(db)) -> Job:
    job = _get_job(session, job_id)
    job.paused = True
    session.commit()
    return job


@app.post("/jobs/{job_id}/resume", response_model=JobOut)
def resume_job(job_id: str, session: Session = Depends(db)) -> Job:
    job = _get_job(session, job_id)
    job.paused = False
    # Re-anchor the schedule so a long pause doesn't fire a backlog.
    job.next_run_at = compute_next_run(job)
    session.commit()
    return job


@app.get("/jobs/{job_id}/runs", response_model=list[RunOut])
def list_runs(job_id: str, limit: int = 50, session: Session = Depends(db)) -> list[Run]:
    _get_job(session, job_id)
    return session.scalars(
        select(Run)
        .where(Run.job_id == job_id)
        .order_by(Run.scheduled_at.desc())
        .limit(min(limit, 200))
    ).all()


@app.get("/jobs/{job_id}/stats", response_model=list[RunStatPoint])
def job_stats(job_id: str, limit: int = 20, session: Session = Depends(db)) -> list[RunStatPoint]:
    """Recent runs as trend points (oldest first, ready to plot)."""
    _get_job(session, job_id)
    runs = session.scalars(
        select(Run)
        .where(Run.job_id == job_id)
        .order_by(Run.scheduled_at.desc())
        .limit(min(limit, 100))
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
def list_alerts(acknowledged: bool | None = None, limit: int = 100, session: Session = Depends(db)) -> list[Alert]:
    stmt = select(Alert).order_by(Alert.created_at.desc()).limit(min(limit, 500))
    if acknowledged is not None:
        stmt = stmt.where(Alert.acknowledged.is_(acknowledged))
    return session.scalars(stmt).all()


@app.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(alert_id: str, session: Session = Depends(db)) -> Alert:
    alert = session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "alert not found")
    alert.acknowledged = True
    session.commit()
    return alert


@app.get("/jobs/{job_id}/lessons", response_model=list[LessonOut])
def job_lessons(job_id: str, session: Session = Depends(db)) -> list[Lesson]:
    _get_job(session, job_id)
    return session.scalars(
        select(Lesson).where(Lesson.job_id == job_id).order_by(Lesson.updated_at.desc()).limit(50)
    ).all()


@app.get("/failure-modes", response_model=list[FailureModeOut])
def failure_modes(job_id: str | None = None, session: Session = Depends(db)) -> list[FailureModeOut]:
    """Failed runs clustered into recurring failure modes."""
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
        for m in cluster_failures(session, job_id=job_id)
    ]


@app.post("/failure-modes/promote", response_model=EvalCaseOut, status_code=201)
def promote_failure_mode(body: PromoteRequest, session: Session = Depends(db)) -> EvalCase:
    """Turn a failure mode into a regression eval case (the flywheel step)."""
    modes = {m.signature: m for m in cluster_failures(session, job_id=body.job_id)}
    mode = modes.get(body.signature)
    if mode is None:
        raise HTTPException(404, "failure mode not found")

    latest = session.get(Run, mode.latest_run_id)
    job = latest.job
    name = f"regression-{mode.signature}"
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
def list_eval_cases(session: Session = Depends(db)) -> list[EvalCase]:
    return session.scalars(select(EvalCase).order_by(EvalCase.created_at)).all()


@app.post("/eval-cases", response_model=EvalCaseOut, status_code=201)
def create_eval_case(body: EvalCaseCreate, session: Session = Depends(db)) -> EvalCase:
    if session.scalar(select(EvalCase).where(EvalCase.name == body.name)):
        raise HTTPException(409, f"eval case named {body.name!r} already exists")
    case = EvalCase(**body.model_dump())
    session.add(case)
    session.commit()
    return case


@app.delete("/eval-cases/{case_id}", status_code=204)
def delete_eval_case(case_id: str, session: Session = Depends(db)) -> None:
    case = session.get(EvalCase, case_id)
    if case is None:
        raise HTTPException(404, "eval case not found")
    session.delete(case)
    session.commit()


@app.get("/runs/{run_id}", response_model=RunDetailOut)
def get_run(run_id: str, session: Session = Depends(db)) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


# Static dashboard — mounted last so API routes take precedence.
_web_dir = Path(__file__).resolve().parent.parent / "web"
if _web_dir.is_dir():  # pragma: no branch
    app.mount("/ui", StaticFiles(directory=_web_dir, html=True), name="ui")
