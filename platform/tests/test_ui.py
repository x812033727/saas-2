import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from ticloud.scheduler.worker import execute_run

from test_api import create_job


def test_root_redirects_to_ui(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/ui/"


def test_ui_serves_dashboard(client):
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert 'id="app"' in resp.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/style.css").status_code == 200


def test_api_module_runs_dashboard_server(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as sock:
        port = sock.getsockname()[1]

    env = os.environ.copy()
    env["TICLOUD_DATABASE_URL"] = f"sqlite:///{tmp_path / 'server.db'}"
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ticloud.api.main",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(f"server exited early ({proc.returncode}): {output}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/ui/", timeout=0.5) as resp:
                    body = resp.read().decode()
                assert 'id="app"' in body
                return
            except Exception as exc:  # pragma: no cover - only used while polling
                last_error = exc
                time.sleep(0.2)
        raise AssertionError(f"server did not answer /ui/: {last_error}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_ui_exposes_approval_controls(client):
    index = client.get("/ui/").text
    app_js = client.get("/ui/app.js").text

    assert "#/approvals" in index
    assert 'name="approval_required"' in app_js
    assert 'api("/approvals")' in app_js
    assert 'data-runact="approve"' in app_js
    assert 'data-runact="reject"' in app_js


def test_ui_exposes_rerun_control(client):
    app_js = client.get("/ui/app.js").text

    assert "TERMINAL_RUN_STATUSES.has(run.status)" in app_js
    assert 'data-runact="rerun"' in app_js
    assert "`/runs/${runBtn.dataset.id}/rerun`" in app_js
    assert "`#/runs/${run.id}`" in app_js


def test_ui_exposes_alert_filters_and_bulk_ack(client):
    index = client.get("/ui/").text
    app_js = client.get("/ui/app.js").text
    style_css = client.get("/ui/style.css").text

    assert 'href="#/alerts/open">Alerts' in index
    assert 'api("/alerts/summary")' in app_js
    assert "summary.unacknowledged" in app_js
    assert 'href="#/alerts/open"' in app_js
    assert 'href="#/alerts/acked"' in app_js
    assert "acknowledged=false" in app_js
    assert "acknowledged=true" in app_js
    assert "data-ack-all" in app_js
    assert ".tabs a.active" in style_css


def test_ui_exposes_job_settings_editor(client):
    app_js = client.get("/ui/app.js").text

    assert 'class="jobsettings"' in app_js
    assert 'method: "PATCH"' in app_js
    assert "`/jobs/${id}`" in app_js
    assert 'name="approval_required"' in app_js
    assert 'name="webhook_url"' in app_js


def test_ui_exposes_manual_lesson_controls(client):
    app_js = client.get("/ui/app.js").text

    assert 'class="lessonform"' in app_js
    assert 'name="title"' in app_js
    assert 'name="content"' in app_js
    assert "`/jobs/${id}/lessons`" in app_js
    assert "data-dellesson" in app_js
    assert "`/jobs/${lessonBtn.dataset.job}/lessons/${lessonBtn.dataset.dellesson}`" in app_js


def test_ui_manual_lesson_payload_is_trimmed_before_submit(client):
    app_js = client.get("/ui/app.js").text

    assert 'title: String(f.get("title") || "").trim()' in app_js
    assert 'content: String(f.get("content") || "").trim()' in app_js


def test_ui_manual_lesson_delete_handler_precedes_generic_job_action(client):
    app_js = client.get("/ui/app.js").text

    lesson_delete = app_js.index('button[data-dellesson]')
    generic_action = app_js.index('button[data-act]')
    assert lesson_delete < generic_action


def test_ui_exposes_job_scoped_failure_mode_controls(client):
    index = client.get("/ui/").text
    app_js = client.get("/ui/app.js").text

    assert 'href="#/failures"' in index
    assert "`/failure-modes?job_id=${encodeURIComponent(id)}`" in app_js
    assert 'data-promote="${esc(m.signature)}" data-job="${esc(job.id)}"' in app_js
    assert "body.job_id = promoteBtn.dataset.job" in app_js
    assert "Regression eval cases" in app_js


def test_overview_includes_last_run(client):
    job = create_job(client, cron=None)
    create_job(client, name="second-job", cron=None)

    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    overview = {j["name"]: j for j in client.get("/overview").json()}
    assert len(overview) == 2
    assert overview["nightly-patrol"]["last_run"]["status"] == "succeeded"
    assert overview["nightly-patrol"]["last_run"]["cost_usd"] > 0
    assert overview["second-job"]["last_run"] is None


def test_job_stats_series(client):
    job = create_job(client, cron=None)
    for _ in range(3):
        run = client.post(f"/jobs/{job['id']}/trigger").json()
        execute_run(run["id"])

    stats = client.get(f"/jobs/{job['id']}/stats").json()
    assert len(stats) == 3
    point = stats[0]
    assert point["status"] == "succeeded"
    assert point["cost_usd"] > 0
    assert point["duration_s"] is not None
    # Oldest first, ready to plot left-to-right.
    assert stats[0]["scheduled_at"] <= stats[-1]["scheduled_at"]

    limited = client.get(f"/jobs/{job['id']}/stats?limit=2").json()
    assert len(limited) == 2
