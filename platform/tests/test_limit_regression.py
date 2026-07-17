from ticloud.models import Alert


def create_job(client, **overrides):
    body = {"name": "limit-regression", "engine": "offline", "cron": "0 2 * * *"}
    body.update(overrides)
    resp = client.post("/jobs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def trigger_runs(client, job_id, count):
    for _ in range(count):
        resp = client.post(f"/jobs/{job_id}/trigger")
        assert resp.status_code == 201, resp.text


def test_negative_runs_limit_is_rejected_instead_of_returning_all_rows(client):
    job = create_job(client)
    trigger_runs(client, job["id"], 3)

    resp = client.get(f"/jobs/{job['id']}/runs?limit=-1")

    assert resp.status_code == 422


def test_negative_stats_limit_is_rejected_instead_of_returning_all_rows(client):
    job = create_job(client)
    trigger_runs(client, job["id"], 3)

    resp = client.get(f"/jobs/{job['id']}/stats?limit=-1")

    assert resp.status_code == 422


def test_negative_alerts_limit_is_rejected_instead_of_returning_all_rows(client, session):
    job = create_job(client)
    for i in range(3):
        session.add(
            Alert(
                job_id=job["id"],
                kind="run_failed",
                message=f"negative limit regression {i}",
            )
        )
    session.commit()

    resp = client.get("/alerts?limit=-1")

    assert resp.status_code == 422
