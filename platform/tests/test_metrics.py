"""Wave 5 B9 — Prometheus /metrics + structured JSON logging."""

import json
import logging

from test_scheduler import make_job
from ticloud.metrics import JsonFormatter, render_metrics
from ticloud.models import Alert, Run, RunStatus


def test_metrics_endpoint_exposition(session, client):
    job = make_job(session)
    session.add(Run(job_id=job.id, status=RunStatus.SUCCEEDED, cost_usd=0.5, tokens_in=100, tokens_out=40))
    session.add(Run(job_id=job.id, status=RunStatus.QUEUED))
    session.add(Alert(job_id=job.id, kind="low_score", message="x", acknowledged=False))
    session.commit()

    body = client.get("/metrics").text
    assert 'ticloud_runs_total{status="succeeded"} 1' in body
    assert 'ticloud_runs_total{status="queued"} 1' in body
    assert 'ticloud_runs_total{status="failed"} 0' in body  # every status present
    assert 'ticloud_jobs{state="active"} 1' in body
    assert "ticloud_alerts_unacknowledged 1" in body
    assert "ticloud_cost_usd_total 0.5" in body
    assert 'ticloud_tokens_total{direction="in"} 100' in body
    # Valid Prometheus exposition: HELP/TYPE precede each metric.
    assert "# TYPE ticloud_runs_total gauge" in body


def test_metrics_empty_db(client):
    body = client.get("/metrics").text
    assert 'ticloud_runs_total{status="running"} 0' in body
    assert "ticloud_cost_usd_total 0" in body


def test_render_metrics_direct(session):
    text = render_metrics(session)
    assert text.endswith("\n")
    assert "# HELP ticloud_jobs" in text


def test_json_formatter_includes_extra_fields():
    fmt = JsonFormatter()
    rec = logging.LogRecord("ticloud.worker", logging.INFO, __file__, 1, "run done", (), None)
    rec.run_id = "abc123"
    rec.job = "nightly"
    out = json.loads(fmt.format(rec))
    assert out["level"] == "INFO"
    assert out["logger"] == "ticloud.worker"
    assert out["msg"] == "run done"
    assert out["run_id"] == "abc123"
    assert out["job"] == "nightly"
    assert "ts" in out


def test_json_formatter_captures_exception():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    out = json.loads(fmt.format(rec))
    assert "boom" in out["exc"]
