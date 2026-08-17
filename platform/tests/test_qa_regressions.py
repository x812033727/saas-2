def test_qa_interval_job_must_clear_interval_before_switching_to_cron(client):
    created = client.post("/jobs", json={"name": "hourly", "interval_seconds": 3600})
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["cron"] is None
    assert job["interval_seconds"] == 3600
    assert job["next_run_at"] is not None

    blocked = client.patch(f"/jobs/{job['id']}", json={"cron": "15 1 * * *"})
    assert blocked.status_code == 422
    assert "mutually exclusive" in blocked.text

    switched = client.patch(
        f"/jobs/{job['id']}",
        json={"interval_seconds": None, "cron": "15 1 * * *"},
    )
    assert switched.status_code == 200, switched.text
    switched_job = switched.json()
    assert switched_job["cron"] == "15 1 * * *"
    assert switched_job["interval_seconds"] is None
    assert switched_job["next_run_at"] is not None


def test_qa_interval_template_cron_override_clears_interval_and_blank_payload_fails(client):
    blank = client.post(
        "/jobs/from-template/ci-babysitter",
        json={"name": "blank-ci", "payload": {"repo_url": "\n\t "}},
    )
    assert blank.status_code == 422
    assert "repo_url" in str(blank.json()["detail"])

    created = client.post(
        "/jobs/from-template/ci-babysitter",
        json={
            "name": "nightly-ci",
            "cron": "5 5 * * *",
            "payload": {"repo_url": "https://example.test/repo"},
        },
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["cron"] == "5 5 * * *"
    assert job["interval_seconds"] is None
    assert job["payload"]["repo_url"] == "https://example.test/repo"
