from pathlib import Path

from sqlalchemy import func, select

from ticloud.models import Alert, EvalCase, Job, Lesson, Run, RunStep, ScoreRecord
from ticloud.scheduler.worker import execute_run


def _create_job(client, name: str) -> dict:
    resp = client.post(
        "/jobs",
        json={"name": name, "engine": "offline", "cron": None},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _trigger_and_finish(client, job_id: str) -> dict:
    resp = client.post(f"/jobs/{job_id}/trigger")
    assert resp.status_code == 201, resp.text
    run = resp.json()
    execute_run(run["id"])
    return run


def test_delete_missing_job_is_404_without_side_effects(client, session):
    job = _create_job(client, "keep-me")
    run = _trigger_and_finish(client, job["id"])
    session.add_all(
        [
            Alert(job_id=job["id"], run_id=run["id"], kind="low_score", message="keep"),
            Lesson(job_id=job["id"], source_run_id=run["id"], title="keep", content="keep"),
            EvalCase(name="keep-case", job_id=job["id"], payload={}),
        ]
    )
    session.commit()

    resp = client.delete("/jobs/not-a-real-job")

    assert resp.status_code == 404
    session.expire_all()
    assert session.get(Job, job["id"]) is not None
    assert session.scalar(select(func.count()).select_from(Run)) == 1
    assert session.scalar(select(func.count()).select_from(RunStep)) > 0
    assert session.scalar(select(func.count()).select_from(ScoreRecord)) > 0
    assert session.scalar(select(func.count()).select_from(Alert)) == 1
    assert session.scalar(select(func.count()).select_from(Lesson)) == 1
    assert session.scalar(select(func.count()).select_from(EvalCase)) == 1


def test_delete_job_removes_only_target_artifacts(client, session):
    victim = _create_job(client, "delete-me")
    survivor = _create_job(client, "survive-me")
    victim_run = _trigger_and_finish(client, victim["id"])
    survivor_run = _trigger_and_finish(client, survivor["id"])
    session.add_all(
        [
            Alert(
                job_id=victim["id"],
                run_id=victim_run["id"],
                kind="low_score",
                message="victim alert",
            ),
            Alert(
                job_id=survivor["id"],
                run_id=survivor_run["id"],
                kind="low_score",
                message="survivor alert",
            ),
            Lesson(
                job_id=victim["id"],
                source_run_id=victim_run["id"],
                title="victim lesson",
                content="delete",
            ),
            Lesson(
                job_id=survivor["id"],
                source_run_id=survivor_run["id"],
                title="survivor lesson",
                content="keep",
            ),
            EvalCase(name="victim-case", job_id=victim["id"], payload={}),
            EvalCase(name="survivor-case", job_id=survivor["id"], payload={}),
            EvalCase(name="global-case", payload={}),
        ]
    )
    session.commit()

    resp = client.delete(f"/jobs/{victim['id']}")

    assert resp.status_code == 204
    session.expire_all()
    assert session.get(Job, victim["id"]) is None
    assert session.get(Job, survivor["id"]) is not None
    assert session.scalar(select(Run).where(Run.job_id == victim["id"])) is None
    assert session.scalar(select(Alert).where(Alert.job_id == victim["id"])) is None
    assert session.scalar(select(Lesson).where(Lesson.job_id == victim["id"])) is None
    assert session.scalar(select(EvalCase).where(EvalCase.job_id == victim["id"])) is None
    assert session.scalar(select(Run).where(Run.job_id == survivor["id"])) is not None
    assert session.scalar(select(RunStep).where(RunStep.run_id == survivor_run["id"])) is not None
    assert session.scalar(select(ScoreRecord).where(ScoreRecord.run_id == survivor_run["id"])) is not None
    assert session.scalar(select(Alert).where(Alert.job_id == survivor["id"])) is not None
    assert session.scalar(select(Lesson).where(Lesson.job_id == survivor["id"])) is not None
    assert session.scalar(select(EvalCase).where(EvalCase.name == "survivor-case")) is not None
    assert session.scalar(select(EvalCase).where(EvalCase.name == "global-case")) is not None


def test_job_scoped_eval_run_filters_enabled_cases_and_rejects_unknown_job(client, session):
    first = _create_job(client, "eval-first")
    second = _create_job(client, "eval-second")
    session.add_all(
        [
            EvalCase(name="first-enabled", job_id=first["id"], payload={}, min_score=0.0),
            EvalCase(
                name="first-disabled",
                job_id=first["id"],
                payload={},
                min_score=0.0,
                enabled=False,
            ),
            EvalCase(name="second-enabled", job_id=second["id"], payload={}, min_score=0.0),
            EvalCase(name="global-enabled", payload={}, min_score=0.0),
        ]
    )
    session.commit()

    missing = client.post("/eval-cases/run", json={"job_id": "not-a-real-job"})
    scoped = client.post("/eval-cases/run", json={"job_id": first["id"]})

    assert missing.status_code == 404
    assert scoped.status_code == 200, scoped.text
    body = scoped.json()
    assert body["total"] == 1
    assert body["failed"] == 0
    assert [case["name"] for case in body["cases"]] == ["first-enabled"]
    assert body["cases"][0]["job_id"] == first["id"]


def test_job_scoped_eval_run_with_no_enabled_cases_is_empty_success(client, session):
    job = _create_job(client, "disabled-only")
    session.add(EvalCase(name="disabled-case", job_id=job["id"], payload={}, enabled=False))
    session.commit()

    resp = client.post("/eval-cases/run", json={"job_id": job["id"]})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"total": 0, "passed": 0, "failed": 0, "cases": []}


def test_ui_destructive_job_delete_confirms_before_api_call():
    app_js = Path("platform/ticloud/web/app.js").read_text()
    delete_handler = app_js.index('const deleteJobBtn = ev.target.closest("button[data-deljob]")')
    confirm_call = app_js.index('confirm("Delete this job and all of its runs?")', delete_handler)
    api_delete = app_js.index('api(`/jobs/${deleteJobBtn.dataset.deljob}`, { method: "DELETE" })')
    generic_action_handler = app_js.index('const btn = ev.target.closest("button[data-act]")')

    assert delete_handler < generic_action_handler
    assert delete_handler < confirm_call < api_delete
    assert 'data-runevals-job="${esc(job.id)}"' in app_js
    assert 'const body = jobId ? { job_id: jobId } : {}' in app_js
