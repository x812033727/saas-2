"""Ti engine adapter tests — fake runners exercise the subprocess protocol.

The real runner needs a Ti checkout with its own venv, so unit tests
monkeypatch ``_runner_command`` to run small stand-in scripts with the test
interpreter. The protocol (stdin config JSON in, JSONL out) is identical, so
everything except Ti itself is exercised: trace bridging, cost/budget,
lessons both ways, retry context, cancellation, and failure reporting.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_scheduler import make_job
from test_worker import run_job_once
from ticloud.config import settings
from ticloud.engine import ti_adapter
from ticloud.models import Lesson, Run, RunStatus
from ticloud.scheduler.queue import claim_next_run, enqueue_manual
from ticloud.scheduler.worker import execute_run

TI_PAYLOAD = {"repo_url": "https://github.com/acme/widgets", "brief": "nightly patrol"}


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    """Install a fake runner script; returns a function that sets its body."""
    monkeypatch.setattr(settings, "ti_path", str(tmp_path))
    script = tmp_path / "fake_runner.py"

    def install(body: str) -> Path:
        script.write_text(
            "import json, os, sys, time\n"
            "config = json.loads(sys.stdin.read())\n"
            "def emit(obj):\n"
            "    sys.stdout.write(json.dumps(obj) + '\\n')\n"
            "    sys.stdout.flush()\n" + body
        )
        monkeypatch.setattr(
            ti_adapter, "_runner_command", lambda ti_path: [sys.executable, str(script)]
        )
        return script

    return install


def _assert_process_gone(pid: int, within_s: float = 5.0) -> None:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"runner process {pid} still alive")


def test_ti_run_succeeds_with_trace(session, fake_runner):
    fake_runner(
        """
emit({"t": "phase", "name": "clarify", "detail": "kickoff"})
emit({"t": "cost", "cost_usd": 0.01, "tokens_in": 500, "tokens_out": 100})
emit({"t": "phase", "name": "implement"})
emit({"t": "step", "role": "critic", "kind": "review", "name": "critic:pm",
      "output": {"verdict": "approve", "text": "ok"}})
emit({"t": "cost", "cost_usd": 0.02, "tokens_in": 800, "tokens_out": 300})
emit({"t": "result", "summary": "Ti workshop completed — PR https://github.com/acme/widgets/pull/7",
      "data": {"pr_url": "https://github.com/acme/widgets/pull/7", "shippable": True}})
"""
    )
    run = run_job_once(session, engine="ti", payload=TI_PAYLOAD)

    assert run.status == RunStatus.SUCCEEDED
    assert run.result["pr_url"] == "https://github.com/acme/widgets/pull/7"
    assert "PR https://" in run.result["summary"]
    names = [(s.role, s.kind, s.name) for s in run.steps]
    assert ("team", "phase", "clarify") in names
    assert ("team", "phase", "implement") in names
    assert ("critic", "review", "critic:pm") in names
    assert all(s.finished_at is not None for s in run.steps)  # phases closed
    assert run.cost_usd == pytest.approx(0.03)
    assert (run.tokens_in, run.tokens_out) == (1300, 400)


def test_ti_config_carries_lessons_and_payload(session, fake_runner, tmp_path, monkeypatch):
    dump = tmp_path / "config.json"
    monkeypatch.setenv("TICLOUD_TEST_DUMP", str(dump))
    fake_runner(
        """
with open(os.environ["TICLOUD_TEST_DUMP"], "w") as f:
    json.dump(config, f)
emit({"t": "result", "summary": "ok", "data": {}})
"""
    )
    job = make_job(
        session,
        engine="ti",
        payload={**TI_PAYLOAD, "workflow": "fast_track", "time_budget_s": 60, "secret": "x"},
    )
    session.add(Lesson(job_id=job.id, title="failure:abc", content="watch the flaky gate"))
    session.commit()
    enqueue_manual(session, job)
    execute_run(claim_next_run(session).id)

    config = json.loads(dump.read_text())
    assert config["repo_url"] == TI_PAYLOAD["repo_url"]
    assert config["workflow"] == "fast_track"
    assert config["time_budget_s"] == 60
    assert "secret" not in config  # only whitelisted keys are forwarded
    assert config["lessons"] == ["failure:abc — watch the flaky gate"]
    assert "previous_error" not in config


def test_ti_retry_carries_previous_error_and_failure_lesson(
    session, fake_runner, tmp_path, monkeypatch
):
    dump = tmp_path / "retry-config.json"
    monkeypatch.setenv("TICLOUD_TEST_DUMP", str(dump))
    fake_runner(
        """
with open(os.environ["TICLOUD_TEST_DUMP"], "w") as f:
    json.dump(config, f)
if not config.get("previous_error"):
    emit({"t": "fatal", "error": "expert crashed: provider quota exhausted"})
    sys.exit(1)
emit({"t": "result", "summary": "recovered", "data": {}})
"""
    )
    job = make_job(session, engine="ti", payload=TI_PAYLOAD, max_retries=1)
    enqueue_manual(session, job)
    execute_run(claim_next_run(session).id)  # attempt 1 fails
    retry = claim_next_run(session)
    assert retry is not None and retry.attempt == 2
    execute_run(retry.id)

    session.expire_all()
    assert session.get(Run, retry.id).status == RunStatus.SUCCEEDED
    config = json.loads(dump.read_text())  # what attempt 2 received
    assert "provider quota exhausted" in config["previous_error"]
    assert any(l.startswith("failure:") for l in config["lessons"])


def test_ti_budget_exceeded_kills_runner(session, fake_runner, tmp_path, monkeypatch):
    pid_file = tmp_path / "runner.pid"
    monkeypatch.setenv("TICLOUD_TEST_DUMP", str(pid_file))
    fake_runner(
        """
open(os.environ["TICLOUD_TEST_DUMP"], "w").write(str(os.getpid()))
emit({"t": "cost", "cost_usd": 999.0, "tokens_in": 1, "tokens_out": 1})
time.sleep(60)
"""
    )
    run = run_job_once(session, engine="ti", payload=TI_PAYLOAD, budget_usd=1.0)

    assert run.status == RunStatus.BUDGET_EXCEEDED
    _assert_process_gone(int(pid_file.read_text()))


def test_ti_timeout_cancels_runner(session, fake_runner, tmp_path, monkeypatch):
    pid_file = tmp_path / "runner.pid"
    monkeypatch.setenv("TICLOUD_TEST_DUMP", str(pid_file))
    fake_runner(
        """
open(os.environ["TICLOUD_TEST_DUMP"], "w").write(str(os.getpid()))
emit({"t": "phase", "name": "implement"})
time.sleep(60)
"""
    )
    run = run_job_once(session, engine="ti", payload=TI_PAYLOAD, timeout_s=1)

    assert run.status == RunStatus.TIMED_OUT
    _assert_process_gone(int(pid_file.read_text()))


def test_ti_fatal_marks_run_failed(session, fake_runner):
    fake_runner(
        """
emit({"t": "phase", "name": "clarify"})
emit({"t": "fatal", "error": "workshop did not complete: PM verdict incomplete"})
sys.exit(1)
"""
    )
    run = run_job_once(session, engine="ti", payload=TI_PAYLOAD, max_retries=0)

    assert run.status == RunStatus.FAILED
    assert "PM verdict incomplete" in run.error
    lessons = session.query(Lesson).filter_by(job_id=run.job_id).all()
    assert any(l.title.startswith("failure:") for l in lessons)


def test_ti_nonzero_exit_and_malformed_lines(session, fake_runner):
    fake_runner(
        """
sys.stdout.write("not json at all\\n")
sys.stdout.flush()
sys.stderr.write("stack trace goes here\\n")
sys.exit(3)
"""
    )
    run = run_job_once(session, engine="ti", payload=TI_PAYLOAD, max_retries=0)

    assert run.status == RunStatus.FAILED
    assert "rc=3" in run.error
    assert "stack trace goes here" in run.error


def test_ti_requires_ti_path(session, monkeypatch):
    monkeypatch.setattr(settings, "ti_path", None)
    run = run_job_once(session, engine="ti", payload=TI_PAYLOAD, max_retries=0)
    assert run.status == RunStatus.FAILED
    assert "TICLOUD_TI_PATH" in run.error


def test_ti_requires_repo_and_brief(session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "ti_path", str(tmp_path))
    run = run_job_once(session, engine="ti", payload={"brief": "x"}, max_retries=0)
    assert run.status == RunStatus.FAILED
    assert "repo_url" in run.error


@pytest.mark.skipif(
    not Path("/opt/ti/.venv/bin/python").exists(),
    reason="needs a real Ti checkout at /opt/ti",
)
def test_ti_runner_selfcheck_against_real_checkout():
    """Integration: the real runner imports Ti's orchestrator surface."""
    cmd = ti_adapter._runner_command("/opt/ti") + ["--selfcheck"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd="/opt/ti")
    assert proc.returncode == 0, proc.stderr[-1500:]
    line = json.loads(proc.stdout.strip().splitlines()[-1])
    assert line == {"t": "selfcheck", "ok": True, "ti_path": "/opt/ti"}
