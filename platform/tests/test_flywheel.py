import json

from sqlalchemy import select

from ticloud.eval.cli import list_cases, run_cases
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
    assert case["name"] == f"regression-{job['id'][:8]}-{sig}"
    assert case["payload"] == {"fail_at": 2}  # inherits the failing payload
    assert case["source_signature"] == sig

    # Idempotent-ish: second promote conflicts instead of duplicating.
    assert client.post("/failure-modes/promote", json={"signature": sig}).status_code == 409

    cases = client.get("/eval-cases").json()
    assert len(cases) == 1
    assert client.delete(f"/eval-cases/{cases[0]['id']}").status_code == 204
    assert client.get("/eval-cases").json() == []


def test_promote_failure_mode_can_target_one_job_when_signature_is_shared(client):
    from test_api import create_job

    job_a = create_job(client, name="flaky-a", cron=None, max_retries=0, payload={"fail_at": 2})
    job_b = create_job(client, name="flaky-b", cron=None, max_retries=0, payload={"fail_at": 2})
    for job in (job_a, job_b):
        run = client.post(f"/jobs/{job['id']}/trigger").json()
        execute_run(run["id"])

    sig_a = client.get(f"/failure-modes?job_id={job_a['id']}").json()[0]["signature"]
    sig_b = client.get(f"/failure-modes?job_id={job_b['id']}").json()[0]["signature"]
    assert sig_a == sig_b

    case_b = client.post(
        "/failure-modes/promote",
        json={"signature": sig_b, "job_id": job_b["id"]},
    ).json()
    case_a = client.post(
        "/failure-modes/promote",
        json={"signature": sig_a, "job_id": job_a["id"]},
    ).json()

    assert case_b["job_id"] == job_b["id"]
    assert case_b["name"] == f"regression-{job_b['id'][:8]}-{sig_b}"
    assert case_a["job_id"] == job_a["id"]
    assert case_a["name"] == f"regression-{job_a['id'][:8]}-{sig_a}"


def test_failure_modes_can_filter_to_recurring_failures(client):
    from test_api import create_job

    repeated = create_job(
        client, name="repeated", cron=None, max_retries=0, payload={"fail_at": 3}
    )
    one_off = create_job(
        client, name="one-off", cron=None, max_retries=0, payload={"fail_at": 0}
    )

    for _ in range(2):
        run = client.post(f"/jobs/{repeated['id']}/trigger").json()
        execute_run(run["id"])
    run = client.post(f"/jobs/{one_off['id']}/trigger").json()
    execute_run(run["id"])

    modes = client.get("/failure-modes").json()
    assert [m["count"] for m in modes] == [2, 1]

    recurring = client.get("/failure-modes?min_count=2").json()
    assert len(recurring) == 1
    assert recurring[0]["count"] == 2

    assert client.get("/failure-modes?min_count=0").status_code == 422


def test_failure_modes_can_filter_to_unpromoted_failures(client):
    from test_api import create_job

    job = create_job(client, cron=None, max_retries=0, payload={"fail_at": 2})
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    mode = client.get("/failure-modes").json()[0]
    assert mode["promoted"] is False
    assert len(client.get("/failure-modes?unpromoted_only=true").json()) == 1

    client.post("/failure-modes/promote", json={"signature": mode["signature"]})

    modes = client.get("/failure-modes").json()
    assert modes[0]["promoted"] is True
    assert client.get("/failure-modes?unpromoted_only=true").json() == []


def test_eval_cli_passes_on_good_case(session):
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.9))
    session.commit()
    assert run_cases() == 0


def test_eval_cli_fails_on_regression(session):
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.9))
    session.add(EvalCase(name="still-broken", engine="offline", payload={"fail_at": 1}, min_score=0.9))
    session.commit()
    assert run_cases() == 1


def test_eval_cli_run_json_output(session, capsys):
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.9))
    session.add(
        EvalCase(name="still-broken", engine="offline", payload={"fail_at": 1}, min_score=0.9)
    )
    session.commit()

    assert run_cases(json_output=True) == 1
    body = json.loads(capsys.readouterr().out)

    assert body["total"] == 2
    assert body["passed"] == 1
    assert body["failed"] == 1
    by_name = {case["name"]: case for case in body["cases"]}
    assert by_name["smoke"]["passed"] is True
    assert by_name["smoke"]["run_status"] == "succeeded"
    assert by_name["still-broken"]["passed"] is False
    assert by_name["still-broken"]["run_status"] == "failed"
    assert "simulated failure" in by_name["still-broken"]["error"]


def test_eval_cli_writes_summary_file(session, tmp_path, capsys):
    summary = tmp_path / "summary.md"
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.9))
    session.add(
        EvalCase(name="still-broken", engine="offline", payload={"fail_at": 1}, min_score=0.9)
    )
    session.commit()

    assert run_cases(json_output=True, summary_file=str(summary)) == 1
    body = json.loads(capsys.readouterr().out)
    text = summary.read_text()

    assert body["failed"] == 1
    assert "### Ti Cloud eval gate" in text
    assert "- Total: 2" in text
    assert "| smoke |" in text
    assert "| still-broken |" in text
    assert "| FAIL |" in text


def test_eval_cli_list_json_output(session, capsys):
    session.add(
        EvalCase(
            name="smoke",
            engine="offline",
            payload={},
            min_score=0.7,
            source_signature="abc123",
            enabled=False,
        )
    )
    session.commit()

    assert list_cases(json_output=True) == 0
    body = json.loads(capsys.readouterr().out)

    assert body["total"] == 1
    assert body["cases"][0]["name"] == "smoke"
    assert body["cases"][0]["min_score"] == 0.7
    assert body["cases"][0]["source_signature"] == "abc123"
    assert body["cases"][0]["enabled"] is False


def test_eval_cases_can_run_via_api(client):
    assert client.post("/eval-cases", json={"name": "smoke", "payload": {}}).status_code == 201
    assert (
        client.post(
            "/eval-cases",
            json={"name": "still-broken", "payload": {"fail_at": 1}},
        ).status_code
        == 201
    )
    off = client.post("/eval-cases", json={"name": "off-switch", "payload": {}}).json()
    assert client.patch(f"/eval-cases/{off['id']}", json={"enabled": False}).status_code == 200

    resp = client.post("/eval-cases/run", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total"] == 2
    assert body["passed"] == 1
    assert body["failed"] == 1
    by_name = {case["name"]: case for case in body["cases"]}
    assert set(by_name) == {"smoke", "still-broken"}
    assert by_name["smoke"]["run_status"] == "succeeded"
    assert by_name["still-broken"]["passed"] is False
    assert "simulated failure" in by_name["still-broken"]["error"]


def test_eval_case_run_api_can_filter_by_job(client):
    from test_api import create_job

    job_a = create_job(client, name="source-a", cron=None)
    job_b = create_job(client, name="source-b", cron=None)
    client.post("/eval-cases", json={"name": "a-case", "job_id": job_a["id"], "payload": {}})
    client.post("/eval-cases", json={"name": "b-case", "job_id": job_b["id"], "payload": {}})

    resp = client.post("/eval-cases/run", json={"job_id": job_a["id"], "min_score": 0.5})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total"] == 1
    assert body["failed"] == 0
    assert body["cases"][0]["name"] == "a-case"
    assert body["cases"][0]["job_id"] == job_a["id"]
    assert body["cases"][0]["min_score"] == 0.5


def test_eval_cli_reuses_eval_job(session):
    session.add(EvalCase(name="smoke", engine="offline", payload={}, min_score=0.5))
    session.commit()
    run_cases()
    run_cases()
    from ticloud.models import Job

    eval_jobs = session.scalars(select(Job).where(Job.name == "eval:smoke")).all()
    assert len(eval_jobs) == 1
    assert len(eval_jobs[0].runs) == 2  # history accumulates on one job


def test_eval_cli_separates_same_name_cases_from_different_jobs(session):
    job_a = make_job(session, name="source-a")
    job_b = make_job(session, name="source-b")
    session.add(
        EvalCase(name="smoke", job_id=job_a.id, engine="offline", payload={}, min_score=0.5)
    )
    session.add(
        EvalCase(name="smoke", job_id=job_b.id, engine="offline", payload={}, min_score=0.5)
    )
    session.commit()

    assert run_cases() == 0

    from ticloud.models import Job

    eval_jobs = session.scalars(select(Job).where(Job.name.like("eval:%:smoke"))).all()
    assert {j.name for j in eval_jobs} == {
        f"eval:{job_a.id[:8]}:smoke",
        f"eval:{job_b.id[:8]}:smoke",
    }


def test_eval_case_can_be_disabled_via_api(client, session):
    case = client.post("/eval-cases", json={"name": "off-switch", "payload": {}}).json()

    updated = client.patch(f"/eval-cases/{case['id']}", json={"enabled": False}).json()
    assert updated["enabled"] is False
    assert run_cases() == 0

    from ticloud.models import Job

    assert session.scalars(select(Job).where(Job.name == "eval:off-switch")).first() is None

    updated = client.patch(f"/eval-cases/{case['id']}", json={"enabled": True}).json()
    assert updated["enabled"] is True
    assert run_cases() == 0
    assert session.scalars(select(Job).where(Job.name == "eval:off-switch")).first() is not None


def test_eval_case_rejects_unknown_source_job(client):
    resp = client.post("/eval-cases", json={"name": "orphan", "job_id": "missing"})
    assert resp.status_code == 404


def test_eval_case_strips_and_rejects_blank_name(client):
    created = client.post("/eval-cases", json={"name": "  smoke  ", "payload": {}})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "smoke"

    assert client.post("/eval-cases", json={"name": "smoke"}).status_code == 409
    assert client.post("/eval-cases", json={"name": "   "}).status_code == 422


def test_lessons_api(client):
    from test_api import create_job

    job = create_job(client, cron=None, max_retries=0, payload={"fail_at": 0})
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    lessons = client.get(f"/jobs/{job['id']}/lessons").json()
    assert len(lessons) == 1
    assert lessons[0]["title"].startswith("failure:")


def test_manual_lessons_api_create_update_and_delete(client):
    from test_api import create_job

    job = create_job(client, cron=None)
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    created = client.post(
        f"/jobs/{job['id']}/lessons",
        json={
            "title": "manual:retry-policy",
            "content": "Back off flaky CI before rerunning.",
            "source_run_id": run["id"],
        },
    )
    assert created.status_code == 201, created.text
    lesson = created.json()
    assert lesson["source_run_id"] == run["id"]

    updated = client.post(
        f"/jobs/{job['id']}/lessons",
        json={
            "title": "manual:retry-policy",
            "content": "Back off flaky CI and preserve failure logs.",
        },
    ).json()
    assert updated["id"] == lesson["id"]
    assert updated["content"] == "Back off flaky CI and preserve failure logs."
    assert updated["source_run_id"] == run["id"]

    listed = client.get(f"/jobs/{job['id']}/lessons").json()
    assert [l["id"] for l in listed] == [lesson["id"]]

    assert client.delete(f"/jobs/{job['id']}/lessons/{lesson['id']}").status_code == 204
    assert client.get(f"/jobs/{job['id']}/lessons").json() == []


def test_manual_lesson_source_run_must_belong_to_job(client):
    from test_api import create_job

    source_job = create_job(client, name="source", cron=None)
    target_job = create_job(client, name="target", cron=None)
    run = client.post(f"/jobs/{source_job['id']}/trigger").json()

    resp = client.post(
        f"/jobs/{target_job['id']}/lessons",
        json={"title": "manual:foreign", "content": "nope", "source_run_id": run["id"]},
    )

    assert resp.status_code == 404
