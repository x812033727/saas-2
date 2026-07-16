from sqlalchemy import select

from ticloud.eval import score_run
from ticloud.models import Alert, Job, Run
from ticloud.scheduler.queue import claim_next_run, enqueue_manual
from ticloud.scheduler.worker import execute_run

from test_scheduler import make_job
from test_worker import run_job_once


def test_successful_run_scores_high(session):
    run = run_job_once(session)
    assert run.score is not None and run.score >= 0.9
    scorers = {s.scorer: s for s in run.scores}
    assert scorers["completion"].passed
    assert scorers["trajectory"].passed
    # QA approve verdicts feed the trajectory scorer.
    assert scorers["trajectory"].detail["review_approval_rate"] == 1.0
    # cost_anomaly skipped: not enough history.
    assert "cost_anomaly" not in scorers


def test_failed_run_scores_zero(session):
    run = run_job_once(session, max_retries=0, payload={"fail_at": 2})
    assert run.score == 0.0  # completion is a required scorer
    scorers = {s.scorer: s for s in run.scores}
    assert not scorers["completion"].passed


def test_stuck_loop_penalized(session):
    # Build a synthetic run whose steps repeat the same (role, name).
    from datetime import datetime, timezone

    from ticloud.models import RunStatus, RunStep

    job = make_job(session)
    run = Run(job_id=job.id, status=RunStatus.SUCCEEDED, result={"summary": "ok"})
    session.add(run)
    session.flush()
    now = datetime.now(timezone.utc)
    for i in range(4):
        session.add(RunStep(run_id=run.id, index=i, role="engineer", name="Retry build",
                            started_at=now, finished_at=now))
    session.commit()

    overall, results = score_run(run, session, {})
    traj = next(r for r in results if r.scorer == "trajectory")
    assert traj.detail["loop_detected"] is True
    assert traj.score <= 0.5


def test_cost_anomaly_detected(session):
    job = make_job(session)
    # Build history: 3 normal runs, then one 10x cost run.
    for mult in (1.0, 1.0, 1.0, 10.0):
        job.payload = {"cost_multiplier": mult, "steps": 3}
        session.commit()
        enqueue_manual(session, job)
        run = claim_next_run(session)
        execute_run(run.id)
        session.expire_all()

    last = session.scalars(select(Run).order_by(Run.scheduled_at.desc())).first()
    anomaly = next(s for s in last.scores if s.scorer == "cost_anomaly")
    assert not anomaly.passed
    assert anomaly.detail["ratio"] > 3


def test_gate_alerts_and_pauses(session):
    """The core Phase 2 loop: bad run -> low score -> alert -> auto-pause."""
    run = run_job_once(
        session,
        max_retries=0,
        payload={"fail_at": 1},
        score_threshold=0.9,
        on_low_score="pause",
    )
    assert run.score == 0.0

    session.expire_all()
    job = session.get(Job, run.job_id)
    assert job.paused is True

    kinds = {a.kind for a in session.scalars(select(Alert)).all()}
    assert kinds == {"run_failed", "low_score", "auto_paused"}


def test_gate_alert_only_does_not_pause(session):
    run = run_job_once(
        session,
        max_retries=0,
        payload={"fail_at": 1},
        score_threshold=0.9,
        on_low_score="alert",
    )
    session.expire_all()
    job = session.get(Job, run.job_id)
    assert job.paused is False
    kinds = {a.kind for a in session.scalars(select(Alert)).all()}
    assert kinds == {"run_failed", "low_score"}


def test_no_gate_when_threshold_unset(session):
    run = run_job_once(session, max_retries=0, payload={"fail_at": 1})
    assert run.score == 0.0  # still scored...
    kinds = {a.kind for a in session.scalars(select(Alert)).all()}
    assert kinds == {"run_failed"}  # ...but no low_score gate alert


def test_judge_skipped_without_api_key(session, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = run_job_once(session, scorers={"judge": {"enabled": True}})
    assert "judge" not in {s.scorer for s in run.scores}
    assert run.score is not None  # rule scorers still produced a baseline


def test_retry_pending_defers_final_alert(session):
    """While a retry is queued, no run_failed alert fires (not final yet)."""
    run_job_once(session, max_retries=1, payload={"fail_at": 0})
    assert session.scalars(select(Alert)).all() == []

    # Drain the retry; now the failure is final and alerts.
    retry = claim_next_run(session)
    execute_run(retry.id)
    session.expire_all()
    kinds = {a.kind for a in session.scalars(select(Alert)).all()}
    assert "run_failed" in kinds


def test_alerts_api(client):
    from test_api import create_job

    job = create_job(client, cron=None, score_threshold=0.9,
                     on_low_score="alert", max_retries=0, payload={"fail_at": 0})
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    alerts = client.get("/alerts?acknowledged=false").json()
    assert len(alerts) == 2  # run_failed + low_score
    first = alerts[0]

    acked = client.post(f"/alerts/{first['id']}/ack").json()
    assert acked["acknowledged"] is True
    assert len(client.get("/alerts?acknowledged=false").json()) == 1

    # Run detail exposes the scorer breakdown; stats expose the score series.
    detail = client.get(f"/runs/{run['id']}").json()
    assert {s["scorer"] for s in detail["scores"]} >= {"completion", "trajectory"}
    stats = client.get(f"/jobs/{job['id']}/stats").json()
    assert stats[0]["score"] == 0.0
