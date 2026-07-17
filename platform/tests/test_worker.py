from sqlalchemy import select

from ticloud.models import Run, RunStatus
from ticloud.scheduler.queue import claim_next_run, enqueue_manual
from ticloud.scheduler.worker import execute_run

from test_scheduler import make_job


def run_job_once(session, **job_kw) -> Run:
    job = make_job(session, **job_kw)
    enqueue_manual(session, job)
    run = claim_next_run(session)
    execute_run(run.id)
    session.expire_all()
    return session.get(Run, run.id)


def test_offline_run_succeeds_with_full_trace(session):
    run = run_job_once(session)

    assert run.status == RunStatus.SUCCEEDED
    assert run.result["summary"] == "workshop completed (offline demo)"
    assert run.finished_at is not None
    # Full Ti-style workshop trace: pm -> engineer -> qa -> team, costs attributed.
    roles = [s.role for s in run.steps]
    assert roles == ["pm", "pm", "engineer", "engineer", "qa", "engineer", "qa", "team"]
    assert all(s.finished_at is not None for s in run.steps)
    assert run.cost_usd > 0 and run.tokens_in > 0


def test_budget_guard_stops_run(session):
    # 1000x cost blows through a $0.01 budget mid-run.
    run = run_job_once(
        session,
        budget_usd=0.01,
        payload={"cost_multiplier": 1000.0},
    )
    assert run.status == RunStatus.BUDGET_EXCEEDED
    assert "exceeds budget" in run.error
    assert 0 < len(run.steps) < 8  # stopped partway, not at the end
    # Deterministic failure: no retry scheduled.
    assert session.scalars(select(Run)).all() == [run]


def test_timeout_guard_cancels_run(session):
    run = run_job_once(session, timeout_s=1, payload={"sleep_s": 30})
    assert run.status == RunStatus.TIMED_OUT
    assert "timeout" in run.error
    # Timeouts are deterministic: no retry scheduled.
    assert session.scalars(select(Run)).all() == [run]


def test_failure_schedules_retry_with_context(session):
    run = run_job_once(session, max_retries=2, payload={"fail_at": 3})
    assert run.status == RunStatus.FAILED
    assert "simulated failure at step 3" in run.error

    retry = session.scalars(select(Run).where(Run.id != run.id)).one()
    assert retry.status == RunStatus.QUEUED
    assert retry.attempt == 2
    # Failure context travels with the retry so engines can adapt.
    assert retry.result["retry_of"] == run.id
    assert "simulated failure" in retry.result["previous_error"]


def test_retries_are_exhausted(session):
    job = make_job(session, max_retries=1, payload={"fail_at": 0})
    enqueue_manual(session, job)

    # Drain queue until nothing is left: attempt 1 + retry = 2 runs total.
    for _ in range(5):
        run = claim_next_run(session)
        if run is None:
            break
        execute_run(run.id)
        session.expire_all()

    runs = session.scalars(select(Run)).all()
    assert len(runs) == 2
    assert all(r.status == RunStatus.FAILED for r in runs)


def test_unknown_engine_fails_cleanly(session):
    job = make_job(session)
    job.engine = "nope"  # bypass API validation to test worker robustness
    session.commit()
    enqueue_manual(session, job)
    run = claim_next_run(session)
    execute_run(run.id)
    session.expire_all()
    run = session.get(Run, run.id)
    assert run.status == RunStatus.FAILED
    assert "unknown engine" in run.error
