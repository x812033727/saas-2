from sqlalchemy import select

from ticloud.eval.cli import run_cases
from ticloud.eval.failures import cluster_failures, error_signature, normalize_error
from ticloud.models import EvalCase, Lesson, Run, RunStatus
from ticloud.scheduler.queue import claim_next_run, enqueue_manual
from ticloud.scheduler.worker import execute_run

from test_scheduler import make_job
from test_worker import run_job_once


# ---------- lessons ----------

def test_failure_records_lesson(session):
    run = run_job_once(session, max_retries=0, payload={"fail_at": 3})
    lesson = session.scalars(select(Lesson)).one()
    assert lesson.job_id == run.job_id
    assert lesson.title.startswith("failure:")
    assert "Implement task-1" in lesson.content  # failing step named
    assert lesson.source_run_id == run.id


def test_repeat_failure_updates_lesson_not_duplicates(session):
    job = make_job(session, max_retries=0, payload={"fail_at": 3})
    for _ in range(3):
        enqueue_manual(session, job)
        execute_run(claim_next_run(session).id)
        session.expire_all()
    assert len(session.scalars(select(Lesson)).all()) == 1


def test_flywheel_end_to_end(session):
    """First run hits the trap and fails; the retry reads the lesson and succeeds."""
    job = make_job(session, max_retries=1, payload={"flaky_fail_at": 2})
    enqueue_manual(session, job)

    first = claim_next_run(session)
    execute_run(first.id)
    session.expire_all()
    assert session.get(Run, first.id).status == RunStatus.FAILED
    assert session.scalars(select(Lesson)).one()  # lesson recorded

    retry = claim_next_run(session)
    execute_run(retry.id)
    session.expire_all()
    retry = session.get(Run, retry.id)
    assert retry.status == RunStatus.SUCCEEDED
    assert retry.result["lessons_applied"]  # learned, not lucky
    assert retry.score >= 0.9


# ---------- failure clustering ----------

def test_error_signature_normalizes_noise():
    a = 'Traceback...\n  File "/tmp/a1b2c3d4e5f6/x.py", line 49, in run\nRuntimeError: failed at step 4: id 0xdeadbeef'
    b = 'Traceback...\n  File "/var/9f8e7d6c5b4a/y.py", line 132, in run\nRuntimeError: failed at step 7: id 0xcafebabe'
    assert error_signature(a) == error_signature(b)
    assert error_signature(a) != error_signature("TimeoutError: exceeded timeout of 30s")
    assert "<n>" in normalize_error(a)


def test_cluster_failures_groups_by_signature(session):
    job = make_job(session, max_retries=0, payload={"fail_at": 3})
    for _ in range(3):
        enqueue_manual(session, job)
        execute_run(claim_next_run(session).id)
        session.expire_all()
    job.payload = {"fail_at": 0}  # different failing step -> different error text
    session.commit()
    enqueue_manual(session, job)
    execute_run(claim_next_run(session).id)
    session.expire_all()

    modes = cluster_failures(session)
    assert len(modes) == 2
    assert modes[0].count == 3  # most frequent first
    assert modes[1].count == 1
    assert modes[0].latest_run_id in modes[0].sample_run_ids


# ---------- promote + eval cases + CLI ----------

def test_promote_failure_mode_to_eval_case(client):
    from test_api import create_job

    job = create_job(client, cron=None, max_retries=0, payload={"fail_at": 2})
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    modes = client.get("/failure-modes").json()
    assert len(modes) == 1
    sig = modes[0]["signature"]

    case = client.post("/failure-modes/promote", json={"signature": sig}).json()
    assert case["name"] == f"regression-{sig}"
    assert case["payload"] == {"fail_at": 2}  # inherits the failing payload
    assert case["source_signature"] == sig

    # Idempotent-ish: second promote conflicts instead of duplicating.
    assert client.post("/failure-modes/promote", json={"signature": sig}).status_code == 409

    cases = client.get("/eval-cases").json()
    assert len(cases) == 1
    assert client.delete(f"/eval-cases/{cases[0]['id']}").status_code == 204
    assert client.get("/eval-cases").json() == []


def test_eval_cli_passes_on_good_case(session):
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.9))
    session.commit()
    assert run_cases() == 0


def test_eval_cli_fails_on_regression(session):
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.9))
    session.add(EvalCase(name="still-broken", engine="offline", payload={"fail_at": 1}, min_score=0.9))
    session.commit()
    assert run_cases() == 1


def test_eval_cli_reuses_eval_job(session):
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.5))
    session.commit()
    run_cases()
    run_cases()
    from ticloud.models import Job

    eval_jobs = session.scalars(select(Job).where(Job.name == "eval:smoke")).all()
    assert len(eval_jobs) == 1
    assert len(eval_jobs[0].runs) == 2  # history accumulates on one job


def test_lessons_api(client):
    from test_api import create_job

    job = create_job(client, cron=None, max_retries=0, payload={"fail_at": 0})
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    lessons = client.get(f"/jobs/{job['id']}/lessons").json()
    assert len(lessons) == 1
    assert lessons[0]["title"].startswith("failure:")
