from ticloud.scheduler.worker import execute_run


def create_job(client, **overrides):
    body = {"name": "nightly-patrol", "engine": "offline", "cron": "0 2 * * *"}
    body.update(overrides)
    resp = client.post("/jobs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_create_job_computes_schedule(client):
    job = create_job(client)
    assert job["next_run_at"] is not None
    assert job["paused"] is False


def test_create_job_strips_and_rejects_blank_name(client):
    job = create_job(client, name="  nightly-patrol  ")
    assert job["name"] == "nightly-patrol"

    resp = client.post("/jobs", json={"name": "   "})
    assert resp.status_code == 422


def test_create_job_rejects_bad_cron(client):
    resp = client.post("/jobs", json={"name": "x", "cron": "not a cron"})
    assert resp.status_code == 422


def test_create_job_rejects_dual_schedule(client):
    resp = client.post(
        "/jobs",
        json={"name": "x", "cron": "0 2 * * *", "interval_seconds": 3600},
    )
    assert resp.status_code == 422
    assert "mutually exclusive" in resp.text


def test_create_job_rejects_unknown_engine(client):
    resp = client.post("/jobs", json={"name": "x", "engine": "warp-drive"})
    assert resp.status_code == 422


def test_duplicate_name_conflicts(client):
    create_job(client)
    resp = client.post("/jobs", json={"name": "nightly-patrol"})
    assert resp.status_code == 409
    trimmed = client.post("/jobs", json={"name": "  nightly-patrol  "})
    assert trimmed.status_code == 409


def test_pause_resume(client):
    job = create_job(client)
    assert client.post(f"/jobs/{job['id']}/pause").json()["paused"] is True
    resumed = client.post(f"/jobs/{job['id']}/resume").json()
    assert resumed["paused"] is False
    assert resumed["next_run_at"] is not None


def test_update_job_requires_clearing_existing_schedule(client):
    job = create_job(client)

    blocked = client.patch(f"/jobs/{job['id']}", json={"interval_seconds": 3600})
    assert blocked.status_code == 422
    assert "mutually exclusive" in blocked.text

    switched = client.patch(
        f"/jobs/{job['id']}",
        json={"cron": None, "interval_seconds": 3600},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["cron"] is None
    assert switched.json()["interval_seconds"] == 3600
    assert switched.json()["next_run_at"] is not None


def test_update_job_strips_and_rejects_blank_name(client):
    job = create_job(client)

    renamed = client.patch(f"/jobs/{job['id']}", json={"name": "  patrol-v2  "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "patrol-v2"

    blank = client.patch(f"/jobs/{job['id']}", json={"name": "   "})
    assert blank.status_code == 422


def test_trigger_execute_and_inspect_trace(client):
    """End-to-end: create -> trigger -> execute -> read the structured trace."""
    job = create_job(client, cron=None)
    run = client.post(f"/jobs/{job['id']}/trigger").json()
    assert run["status"] == "queued"

    execute_run(run["id"])

    detail = client.get(f"/runs/{run['id']}").json()
    assert detail["status"] == "succeeded"
    assert detail["cost_usd"] > 0
    steps = detail["steps"]
    assert [s["role"] for s in steps][:2] == ["pm", "pm"]
    assert all(s["finished_at"] for s in steps)

    runs = client.get(f"/jobs/{job['id']}/runs").json()
    assert len(runs) == 1 and runs[0]["id"] == run["id"]


def test_limit_validation_for_runs_stats_and_alerts(client):
    job = create_job(client)
    cases = [
        (f"/jobs/{job['id']}/runs", -1, 422),
        (f"/jobs/{job['id']}/runs", 0, 422),
        (f"/jobs/{job['id']}/runs", 201, 422),
        (f"/jobs/{job['id']}/runs", 1, 200),
        (f"/jobs/{job['id']}/stats", -1, 422),
        (f"/jobs/{job['id']}/stats", 0, 422),
        (f"/jobs/{job['id']}/stats", 101, 422),
        (f"/jobs/{job['id']}/stats", 1, 200),
        ("/alerts", -1, 422),
        ("/alerts", 0, 422),
        ("/alerts", 501, 422),
        ("/alerts", 1, 200),
    ]

    for path, limit, expected_status in cases:
        resp = client.get(path, params={"limit": limit})
        assert resp.status_code == expected_status, (path, limit, resp.text)


def test_missing_resources_404(client):
    assert client.get("/jobs/nope").status_code == 404
    assert client.get("/runs/nope").status_code == 404
    assert client.post("/jobs/nope/trigger").status_code == 404
