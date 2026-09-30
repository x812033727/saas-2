import pytest
from sqlalchemy import select

from test_tenancy import _mint_tenant
from ticloud.config import settings
from ticloud.models import Alert, EvalCase, Job, Lesson, Run


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def test_delete_job_cleans_only_owned_job_history_in_hosted_mode(client, hosted, session):
    _, _, auth_a = _mint_tenant(client, "qa-delete-a")
    _, _, auth_b = _mint_tenant(client, "qa-delete-b")

    job_a = client.post("/jobs", json={"name": "shared-name"}, headers=auth_a).json()
    job_b = client.post("/jobs", json={"name": "shared-name"}, headers=auth_b).json()
    run_a = client.post(f"/jobs/{job_a['id']}/trigger", headers=auth_a).json()
    run_b = client.post(f"/jobs/{job_b['id']}/trigger", headers=auth_b).json()

    session.add_all(
        [
            Alert(
                job_id=job_a["id"],
                run_id=run_a["id"],
                kind="low_score",
                message="delete me",
            ),
            Lesson(job_id=job_a["id"], title="manual:a", content="delete me"),
            EvalCase(
                name="tenant-a-regression",
                job_id=job_a["id"],
                engine="offline",
                payload={"owner": "a"},
            ),
            Alert(
                job_id=job_b["id"],
                run_id=run_b["id"],
                kind="low_score",
                message="keep me",
            ),
            Lesson(job_id=job_b["id"], title="manual:b", content="keep me"),
            EvalCase(
                name="tenant-b-regression",
                job_id=job_b["id"],
                engine="offline",
                payload={"owner": "b"},
            ),
        ]
    )
    session.commit()

    deleted = client.delete(f"/jobs/{job_a['id']}", headers=auth_a)

    assert deleted.status_code == 204, deleted.text
    assert session.get(Job, job_a["id"]) is None
    assert session.get(Run, run_a["id"]) is None
    assert session.get(Job, job_b["id"]) is not None
    assert session.get(Run, run_b["id"]) is not None
    assert session.scalars(select(Alert).where(Alert.job_id == job_a["id"])).all() == []
    assert session.scalars(select(Lesson).where(Lesson.job_id == job_a["id"])).all() == []
    assert session.scalars(select(EvalCase).where(EvalCase.job_id == job_a["id"])).all() == []

    assert [a["message"] for a in client.get("/alerts", headers=auth_b).json()] == ["keep me"]
    assert [c["name"] for c in client.get("/eval-cases", headers=auth_b).json()] == [
        "tenant-b-regression"
    ]
    assert [
        lesson["title"]
        for lesson in client.get(f"/jobs/{job_b['id']}/lessons", headers=auth_b).json()
    ] == ["manual:b"]
    assert client.get("/alerts", headers=auth_a).json() == []
    assert client.get("/eval-cases", headers=auth_a).json() == []
