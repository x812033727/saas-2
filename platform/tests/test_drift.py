"""Wave 5 A3 — drift depth: step-count trend + score-regression alert."""

from datetime import datetime, timedelta, timezone

from test_api import create_job
from test_scheduler import make_job
from ticloud.models import Alert, Run, RunStatus, RunStep
from ticloud.scheduler.worker import _maybe_regression_alert


def test_stats_include_step_counts(session, client):
    job = make_job(session)
    run = Run(job_id=job.id, status=RunStatus.SUCCEEDED, scheduled_at=datetime.now(timezone.utc))
    session.add(run)
    session.commit()
    for i in range(3):
        session.add(RunStep(run_id=run.id, index=i, role="pm", name=f"s{i}"))
    session.commit()

    stats = client.get(f"/jobs/{job.id}/stats").json()
    assert stats[-1]["steps"] == 3


def test_stats_step_count_zero_when_no_steps(session, client):
    job = make_job(session)
    session.add(Run(job_id=job.id, status=RunStatus.SUCCEEDED, scheduled_at=datetime.now(timezone.utc)))
    session.commit()
    assert client.get(f"/jobs/{job.id}/stats").json()[-1]["steps"] == 0


def _scored_run(session, job_id, score, when):
    run = Run(job_id=job_id, status=RunStatus.SUCCEEDED, score=score, scheduled_at=when)
    session.add(run)
    session.commit()
    return run


def test_regression_alert_fires_on_decline(session):
    job = make_job(session)
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        _scored_run(session, job.id, 0.95, base + timedelta(minutes=i))  # healthy history
    # A markedly lower score, still (say) above any absolute threshold.
    low = _scored_run(session, job.id, 0.5, base + timedelta(minutes=10))

    _maybe_regression_alert(session, low, 0.5)
    alerts = session.query(Alert).filter_by(job_id=job.id, kind="score_regression").all()
    assert len(alerts) == 1
    assert "degrading" in alerts[0].message


def test_regression_needs_history_and_dedupes(session):
    job = make_job(session)
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    # Too little history → no alert.
    low1 = _scored_run(session, job.id, 0.5, base)
    _maybe_regression_alert(session, low1, 0.5)
    assert session.query(Alert).filter_by(kind="score_regression").count() == 0

    # Build history, then two low runs → only one unacked alert.
    for i in range(1, 5):
        _scored_run(session, job.id, 0.95, base + timedelta(minutes=i))
    a = _scored_run(session, job.id, 0.4, base + timedelta(minutes=6))
    _maybe_regression_alert(session, a, 0.4)
    b = _scored_run(session, job.id, 0.4, base + timedelta(minutes=7))
    _maybe_regression_alert(session, b, 0.4)
    assert session.query(Alert).filter_by(kind="score_regression").count() == 1


def test_no_regression_when_stable(session):
    job = make_job(session)
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        _scored_run(session, job.id, 0.9, base + timedelta(minutes=i))
    stable = _scored_run(session, job.id, 0.88, base + timedelta(minutes=6))
    _maybe_regression_alert(session, stable, 0.88)  # within delta
    assert session.query(Alert).filter_by(kind="score_regression").count() == 0
