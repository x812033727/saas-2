"""Wave 5 B3 — SSE trace streaming.

Only terminal runs are exercised via the sync test client: their generator
emits the buffered steps + a `done` event and returns immediately (a
still-running run would tail forever, which is the point in the browser).
"""

from datetime import datetime, timezone

from test_scheduler import make_job
from ticloud.models import Run, RunStatus, RunStep


def _terminal_run_with_steps(session, n, status=RunStatus.SUCCEEDED):
    job = make_job(session)
    run = Run(job_id=job.id, status=status, scheduled_at=datetime.now(timezone.utc))
    session.add(run)
    session.commit()
    for i in range(n):
        session.add(RunStep(run_id=run.id, index=i, role="pm", name=f"step-{i}", cost_usd=0.01))
    session.commit()
    return run


def test_sse_streams_buffered_steps_then_done(session, client):
    run = _terminal_run_with_steps(session, 3)
    resp = client.get(f"/runs/{run.id}/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert body.count("event: step") == 3
    assert "step-0" in body and "step-2" in body
    assert "event: done" in body
    assert "succeeded" in body


def test_sse_terminal_run_no_steps_just_done(session, client):
    run = _terminal_run_with_steps(session, 0, status=RunStatus.FAILED)
    body = client.get(f"/runs/{run.id}/events").text
    assert "event: step" not in body
    assert "event: done" in body
    assert "failed" in body


def test_sse_missing_run_404(client):
    assert client.get("/runs/nope/events").status_code == 404


def test_sse_step_payload_shape(session, client):
    import json

    run = _terminal_run_with_steps(session, 1)
    body = client.get(f"/runs/{run.id}/events").text
    # Extract the first step's data line.
    data_line = next(
        line[len("data: "):] for line in body.splitlines()
        if line.startswith("data: ") and "step-0" in line
    )
    payload = json.loads(data_line)
    assert payload["index"] == 0
    assert payload["role"] == "pm"
    assert payload["name"] == "step-0"
    assert payload["cost_usd"] == 0.01
    assert set(payload) >= {"index", "role", "kind", "name", "started_at", "finished_at", "output"}
