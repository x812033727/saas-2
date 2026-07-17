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


def test_create_job_rejects_bad_cron(client):
    resp = client.post("/jobs", json={"name": "x", "cron": "not a cron"})
    assert resp.status_code == 422


def test_create_job_rejects_unknown_engine(client):
    resp = client.post("/jobs", json={"name": "x", "engine": "warp-drive"})
    assert resp.status_code == 422


def test_duplicate_name_conflicts(client):
    create_job(client)
    resp = client.post("/jobs", json={"name": "nightly-patrol"})
    assert resp.status_code == 409


def test_pause_resume(client):
    job = create_job(client)
    assert client.post(f"/jobs/{job['id']}/pause").json()["paused"] is True
    resumed = client.post(f"/jobs/{job['id']}/resume").json()
    assert resumed["paused"] is False
    assert resumed["next_run_at"] is not None


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
