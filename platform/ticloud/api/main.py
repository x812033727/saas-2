from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..db import SessionLocal, init_db
from ..models import Job, Run
from ..scheduler.cron import compute_next_run
from ..scheduler.queue import enqueue_manual
from .schemas import (
    JobCreate,
    JobOut,
    JobWithLastRun,
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
            scheduled_at=r.scheduled_at,
        )
        for r in runs
    ]
    return list(reversed(points))


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
