"""Wave 3 A1 — flagship job templates."""

from ticloud import templates
from ticloud.models import Job


def test_list_templates_exposes_flagships(client):
    got = client.get("/templates").json()
    ids = {t["id"] for t in got}
    assert {"nightly-repo-patrol", "dependency-upgrade", "ci-babysitter", "demo-workshop"} <= ids
    patrol = next(t for t in got if t["id"] == "nightly-repo-patrol")
    assert patrol["engine"] == "ti"
    assert "repo_url" in patrol["required_payload"]


def test_from_template_merges_defaults_and_overrides(session, client):
    body = {"name": "my-patrol", "payload": {"repo_url": "https://github.com/me/repo"}}
    r = client.post("/jobs/from-template/nightly-repo-patrol", json=body)
    assert r.status_code == 201
    job = r.json()
    assert job["engine"] == "ti"
    assert job["cron"] == "0 3 * * *"  # template default
    assert job["score_threshold"] == 0.6
    assert job["payload"]["repo_url"] == "https://github.com/me/repo"
    assert job["payload"]["brief"]  # template skeleton preserved
    assert job["next_run_at"] is not None  # scheduled


def test_from_template_cron_override(session, client):
    r = client.post(
        "/jobs/from-template/nightly-repo-patrol",
        json={"name": "p", "cron": "30 6 * * *", "payload": {"repo_url": "x"}},
    )
    assert r.json()["cron"] == "30 6 * * *"


def test_from_template_missing_required_payload_422(client):
    r = client.post("/jobs/from-template/nightly-repo-patrol", json={"name": "p"})
    assert r.status_code == 422
    assert "repo_url" in str(r.json()["detail"])


def test_offline_template_needs_no_payload(session, client):
    r = client.post("/jobs/from-template/demo-workshop", json={"name": "demo"})
    assert r.status_code == 201
    assert r.json()["engine"] == "offline"


def test_from_template_unknown_404(client):
    assert client.post("/jobs/from-template/nope", json={"name": "x"}).status_code == 404


def test_from_template_name_conflict_409(session, client):
    client.post("/jobs/from-template/demo-workshop", json={"name": "dup"})
    r = client.post("/jobs/from-template/demo-workshop", json={"name": "dup"})
    assert r.status_code == 409


def test_ci_babysitter_is_interval_scheduled(session, client):
    r = client.post(
        "/jobs/from-template/ci-babysitter", json={"name": "ci", "payload": {"repo_url": "x"}}
    )
    job = r.json()
    assert job["interval_seconds"] == 3600 and job["cron"] is None


def test_build_job_fields_unit():
    tpl = templates.get_template("nightly-repo-patrol")
    fields = templates.build_job_fields(tpl, "n", None, {"repo_url": "r", "extra": "e"})
    assert fields["name"] == "n"
    assert fields["payload"]["repo_url"] == "r"
    assert fields["payload"]["extra"] == "e"
    assert fields["payload"]["brief"]  # merged over skeleton
    assert templates.missing_required(tpl, fields["payload"]) == []
    assert templates.missing_required(tpl, {"repo_url": ""}) == ["repo_url"]
