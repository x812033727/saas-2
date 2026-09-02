import json
import os
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

from ticloud.eval.cli import run_cases
from ticloud.models import EvalCase

from test_scheduler import make_job


ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "platform"


def test_qa_empty_eval_set_json_and_summary_are_not_silent(session, tmp_path, capsys):
    summary = tmp_path / "summary.md"

    assert run_cases(json_output=True, summary_file=str(summary)) == 0
    stdout = capsys.readouterr().out.strip()
    body = json.loads(stdout)

    assert stdout.startswith("{")
    assert "no eval cases to run" not in stdout
    assert body == {"total": 0, "passed": 0, "failed": 0, "cases": []}

    text = summary.read_text()
    assert "### Ti Cloud eval gate" in text
    assert "- Total: 0" in text
    assert "| Case | Score | Min | Result |" in text


def test_qa_job_filter_json_and_summary_only_report_selected_case(session, tmp_path, capsys):
    good_job = make_job(session, name="qa-good-job", payload={})
    bad_job = make_job(session, name="qa-bad-job", payload={"fail_at": 1})
    session.add(
        EvalCase(
            name="qa-good-case",
            job_id=good_job.id,
            engine="offline",
            payload={},
            min_score=0.9,
        )
    )
    session.add(
        EvalCase(
            name="qa-bad-case",
            job_id=bad_job.id,
            engine="offline",
            payload={"fail_at": 1},
            min_score=0.9,
        )
    )
    session.commit()
    summary = tmp_path / "filtered-summary.md"

    assert run_cases(job_id=bad_job.id, json_output=True, summary_file=str(summary)) == 1
    body = json.loads(capsys.readouterr().out)

    assert body["total"] == 1
    assert body["passed"] == 0
    assert body["failed"] == 1
    assert body["cases"][0]["name"] == "qa-bad-case"
    assert body["cases"][0]["job_id"] == bad_job.id
    assert body["cases"][0]["run_status"] == "failed"
    assert "simulated failure" in body["cases"][0]["error"]

    text = summary.read_text()
    assert "| qa-bad-case |" in text
    assert "| qa-good-case |" not in text


def test_qa_module_cli_json_and_summary_work_from_clean_process(tmp_path):
    db_path = tmp_path / "qa-cli.db"
    summary = tmp_path / "github-step-summary.md"
    env = os.environ.copy()
    env["TICLOUD_DATABASE_URL"] = f"sqlite:///{db_path}"
    seed = dedent(
        """
        from ticloud.db import get_session, init_db
        from ticloud.models import EvalCase

        init_db()
        session = get_session()
        session.add(EvalCase(name="qa-cli-smoke", engine="offline", payload={}, min_score=0.9))
        session.commit()
        session.close()
        """
    )

    seed_proc = subprocess.run(
        [sys.executable, "-c", seed],
        cwd=PLATFORM,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert seed_proc.returncode == 0, seed_proc.stderr

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ticloud.eval.cli",
            "run",
            "--json",
            "--summary-file",
            str(summary),
        ],
        cwd=PLATFORM,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["total"] == 1
    assert body["failed"] == 0
    assert body["cases"][0]["name"] == "qa-cli-smoke"
    assert body["cases"][0]["passed"] is True
    assert "CASE" not in proc.stdout
    assert "| qa-cli-smoke |" in summary.read_text()


def test_qa_eval_gate_workflow_and_action_do_not_duplicate_full_pytest():
    workflow = (ROOT / ".github/workflows/eval-gate.yml").read_text()
    action = (ROOT / "action.yml").read_text()

    assert "python -m pytest" not in workflow
    assert "python -m pytest" not in action
    assert "Seed smoke eval case" in workflow
    assert 'python -m ticloud.eval.cli run --json --summary-file "$GITHUB_STEP_SUMMARY"' in workflow
    assert '[ -n "${GITHUB_STEP_SUMMARY:-}" ] && args+=(--summary-file "$GITHUB_STEP_SUMMARY")' in action
    assert 'python -m ticloud.eval.cli run "${args[@]}" --json' in action
