"""Shared eval-case execution for CLI and API entrypoints."""

from sqlalchemy import select
from sqlalchemy.sql import ColumnElement
from sqlalchemy.orm import Session

from ..models import EvalCase, Job, Run
from ..scheduler.queue import enqueue_manual
from ..scheduler.worker import execute_run


def _tenant_filter(tenant_id: str | None) -> ColumnElement[bool]:
    return Job.tenant_id == tenant_id if tenant_id is not None else Job.tenant_id.is_(None)


def _eval_job(session: Session, case: EvalCase) -> Job:
    prefix = f"eval:{case.job_id[:8]}:" if case.job_id else "eval:"
    name = f"{prefix}{case.name[:200 - len(prefix)]}"
    source_job = session.get(Job, case.job_id) if case.job_id else None
    tenant_id = source_job.tenant_id if source_job is not None else None
    job = session.scalar(select(Job).where(Job.name == name, _tenant_filter(tenant_id)))
    if job is None:
        job = Job(
            name=name,
            tenant_id=tenant_id,
            engine=case.engine,
            payload=case.payload,
            max_retries=0,
        )
        session.add(job)
    else:
        job.tenant_id = tenant_id
        job.engine = case.engine
        job.payload = case.payload
    session.commit()
    return job


def _status_value(status) -> str:
    return getattr(status, "value", status)


def eval_cases_payload(
    session: Session,
    cases: list[EvalCase],
    min_score_override: float | None = None,
) -> dict:
    failures = 0
    results = []
    for case in cases:
        job = _eval_job(session, case)
        run = enqueue_manual(session, job)
        execute_run(run.id)
        session.expire_all()
        run = session.get(Run, run.id)

        minimum = min_score_override if min_score_override is not None else case.min_score
        score = run.score if run.score is not None else 0.0
        ok = score >= minimum
        failures += 0 if ok else 1
        results.append(
            {
                "id": case.id,
                "name": case.name,
                "job_id": case.job_id,
                "engine": case.engine,
                "run_id": run.id,
                "run_status": _status_value(run.status),
                "score": score,
                "min_score": minimum,
                "passed": ok,
                "error": run.error,
            }
        )

    return {
        "total": len(cases),
        "passed": len(cases) - failures,
        "failed": failures,
        "cases": results,
    }
