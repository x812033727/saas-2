from ticloud.scheduler.worker import execute_run

from test_api import create_job


def test_root_redirects_to_ui(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/ui/"


def test_ui_serves_dashboard(client):
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert 'id="app"' in resp.text
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/style.css").status_code == 200


def test_overview_includes_last_run(client):
    job = create_job(client, cron=None)
    create_job(client, name="second-job", cron=None)

    run = client.post(f"/jobs/{job['id']}/trigger").json()
    execute_run(run["id"])

    overview = {j["name"]: j for j in client.get("/overview").json()}
    assert len(overview) == 2
    assert overview["nightly-patrol"]["last_run"]["status"] == "succeeded"
    assert overview["nightly-patrol"]["last_run"]["cost_usd"] > 0
    assert overview["second-job"]["last_run"] is None


def test_job_stats_series(client):
    job = create_job(client, cron=None)
    for _ in range(3):
        run = client.post(f"/jobs/{job['id']}/trigger").json()
        execute_run(run["id"])

    stats = client.get(f"/jobs/{job['id']}/stats").json()
    assert len(stats) == 3
    point = stats[0]
    assert point["status"] == "succeeded"
    assert point["cost_usd"] > 0
    assert point["duration_s"] is not None
    # Oldest first, ready to plot left-to-right.
    assert stats[0]["scheduled_at"] <= stats[-1]["scheduled_at"]

    limited = client.get(f"/jobs/{job['id']}/stats?limit=2").json()
    assert len(limited) == 2
