from datetime import datetime, timedelta, timezone

from test_api import create_job
from ticloud.models import Run, RunStatus


def test_qa_reap_stale_without_job_id_is_precise_and_idempotent(client, session):
    job_a = create_job(client, name="qa-reap-a", cron=None, timeout_s=1)
    job_b = create_job(client, name="qa-reap-b", cron=None, timeout_s=1)
    now = datetime.now(timezone.utc)

    stale_started = Run(
        job_id=job_a["id"],
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=30),
        started_at=now - timedelta(seconds=30),
    )
    stale_scheduled_only = Run(
        job_id=job_b["id"],
        status=RunStatus.RUNNING,
        scheduled_at=now - timedelta(seconds=30),
        started_at=None,
    )
    fresh_running = Run(
        job_id=job_a["id"],
        status=RunStatus.RUNNING,
        scheduled_at=now,
        started_at=now,
    )
    old_failed = Run(
        job_id=job_b["id"],
        status=RunStatus.FAILED,
        scheduled_at=now - timedelta(seconds=30),
        started_at=now - timedelta(seconds=30),
        finished_at=now - timedelta(seconds=20),
        error="already failed",
    )
    old_queued = Run(
        job_id=job_b["id"],
        status=RunStatus.QUEUED,
        scheduled_at=now - timedelta(seconds=30),
    )
    session.add_all(
        [stale_started, stale_scheduled_only, fresh_running, old_failed, old_queued]
    )
    session.commit()

    resp = client.post("/runs/reap-stale")

    assert resp.status_code == 200, resp.text
    assert {run["id"] for run in resp.json()} == {
        stale_started.id,
        stale_scheduled_only.id,
    }

    session.expire_all()
    assert session.get(Run, stale_started.id).status == RunStatus.TIMED_OUT
    assert session.get(Run, stale_scheduled_only.id).status == RunStatus.TIMED_OUT
    assert session.get(Run, fresh_running.id).status == RunStatus.RUNNING
    assert session.get(Run, old_failed.id).status == RunStatus.FAILED
    assert session.get(Run, old_queued.id).status == RunStatus.QUEUED
    assert "exceeded timeout" in session.get(Run, stale_started.id).error

    summary = client.get("/runs/summary").json()
    assert summary["stale_running"] == 0
    assert summary["by_status"]["running"] == 1
    assert summary["by_status"]["timed_out"] == 2

    second_resp = client.post("/runs/reap-stale")
    assert second_resp.status_code == 200, second_resp.text
    assert second_resp.json() == []


def test_qa_ui_reap_stale_buttons_post_global_or_encoded_job_scope(client):
    app_js = client.get("/ui/app.js").text

    assert '<button data-reap-stale>Reap stale</button>' in app_js
    assert 'button[data-reap-stale]' in app_js
    assert 'const job = reapBtn.dataset.job;' in app_js
    assert 'const suffix = job ? `?job_id=${encodeURIComponent(job)}` : "";' in app_js
    assert 'api(`/runs/reap-stale${suffix}`, { method: "POST" })' in app_js
    assert 'data-reap-stale data-job="${esc(job.id)}"' in app_js
