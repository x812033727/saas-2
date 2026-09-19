from sqlalchemy import select

from ticloud.models import EvalCase
from ticloud.scheduler.worker import execute_run


def _create_job(client, *, name, payload=None):
    response = client.post(
        "/jobs",
        json={
            "name": name,
            "engine": "offline",
            "cron": None,
            "max_retries": 0,
            "payload": payload or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _record_failure(client, job_id):
    response = client.post(f"/jobs/{job_id}/trigger")
    assert response.status_code == 201, response.text
    execute_run(response.json()["id"])


def test_qa_promote_trims_signature_and_job_id_before_lookup(client):
    job = _create_job(client, name="qa-promote-trim-job", payload={"fail_at": 2})
    _record_failure(client, job["id"])
    mode = client.get("/failure-modes", params={"job_id": job["id"]}).json()[0]

    response = client.post(
        "/failure-modes/promote",
        json={
            "signature": f" \n{mode['signature']}\t ",
            "job_id": f"  {job['id']}\n",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["job_id"] == job["id"]
    assert body["source_signature"] == mode["signature"]


def test_qa_blank_promote_signature_is_422_and_does_not_create_case(
    client, session
):
    job = _create_job(client, name="qa-promote-blank", payload={"fail_at": 1})
    _record_failure(client, job["id"])

    response = client.post(
        "/failure-modes/promote",
        json={"signature": " \t\n ", "job_id": job["id"]},
    )

    assert response.status_code == 422, response.text
    assert session.scalars(select(EvalCase)).all() == []


def test_qa_eval_case_create_trims_job_id_and_blank_job_id_is_422(
    client, session
):
    job = _create_job(client, name="qa-eval-create-trim")

    created = client.post(
        "/eval-cases",
        json={"name": "qa-trimmed-case", "job_id": f"\t{job['id']}  "},
    )
    blank = client.post(
        "/eval-cases",
        json={"name": "qa-blank-job-case", "job_id": " \n\t "},
    )

    assert created.status_code == 201, created.text
    assert created.json()["job_id"] == job["id"]
    assert blank.status_code == 422, blank.text
    assert (
        session.scalars(
            select(EvalCase).where(EvalCase.name == "qa-blank-job-case")
        ).all()
        == []
    )


def test_qa_eval_run_trims_job_id_and_blank_job_id_is_422(client):
    job = _create_job(client, name="qa-eval-run-trim", payload={"fail_at": 0})
    created = client.post(
        "/eval-cases",
        json={"name": "qa-run-case", "job_id": job["id"], "payload": {"fail_at": 0}},
    )
    assert created.status_code == 201, created.text

    trimmed = client.post(
        "/eval-cases/run",
        json={"job_id": f" \n{job['id']}\t ", "min_score": 0.1},
    )
    blank = client.post("/eval-cases/run", json={"job_id": " \n\t "})

    assert trimmed.status_code == 200, trimmed.text
    assert trimmed.json()["total"] == 1
    assert trimmed.json()["cases"][0]["job_id"] == job["id"]
    assert blank.status_code == 422, blank.text
