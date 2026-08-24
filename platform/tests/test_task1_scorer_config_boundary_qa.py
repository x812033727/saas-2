import pytest
from sqlalchemy import select

from ticloud.models import Alert, Job

from test_api import create_job
from test_worker import run_job_once


def test_api_rejects_scorers_container_that_is_not_object(client):
    for index, bad_scorers in enumerate(([], "enabled", 1, 0, True, False)):
        resp = client.post(
            "/jobs",
            json={"name": f"bad-scorers-{index}", "scorers": bad_scorers},
        )

        assert resp.status_code == 422, resp.text


def test_update_rejects_bad_scorers_without_mutating_existing_config(client):
    job = create_job(
        client,
        cron=None,
        scorers={"completion": {"enabled": True}},
    )

    for bad_scorers in ([], "enabled", 1, 0, True, False):
        resp = client.patch(f"/jobs/{job['id']}", json={"scorers": bad_scorers})

        assert resp.status_code == 422, resp.text
        persisted = client.get(f"/jobs/{job['id']}").json()
        assert persisted["scorers"] == {"completion": {"enabled": True}}


@pytest.mark.parametrize(
    "bad_scorers",
    [[], "", 0, False, True, "enabled"],
    ids=["empty-list", "empty-string", "zero", "false", "true", "string"],
)
def test_worker_fails_closed_for_falsey_malformed_scorers_from_db(session, bad_scorers):
    run = run_job_once(
        session,
        scorers=bad_scorers,
        score_threshold=0.9,
        on_low_score="pause",
    )

    session.expire_all()
    job = session.get(Job, run.job_id)
    scorer_records = {record.scorer: record for record in run.scores}
    alert_kinds = {alert.kind for alert in session.scalars(select(Alert)).all()}

    assert run.score == 0.0
    assert scorer_records["scorers"].passed is False
    assert scorer_records["scorers"].detail["error"] == "scorers config must be an object"
    assert job.paused is True
    assert {"low_score", "auto_paused"} <= alert_kinds
