import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ticloud.api.main import app, db
from ticloud.config import settings
from ticloud.models import EvalCase


ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def qa_hosted(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()
    key = client.post(
        f"/admin/tenants/{tenant['id']}/keys", json={"name": "qa"}, headers=ADMIN
    ).json()
    return {"Authorization": f"Bearer {key['secret']}"}


def test_qa_eval_case_blank_name_is_rejected_without_persisting(client, session):
    resp = client.post("/eval-cases", json={"name": " \t\n "})

    assert resp.status_code == 422, resp.text
    assert session.scalars(select(EvalCase)).all() == []


def test_qa_eval_case_unknown_job_id_is_rejected_without_persisting(client, session):
    resp = client.post(
        "/eval-cases",
        json={"name": "orphan-regression", "job_id": "missing"},
    )

    assert resp.status_code == 404, resp.text
    assert (
        session.scalars(
            select(EvalCase).where(EvalCase.name == "orphan-regression")
        ).all()
        == []
    )


def test_qa_health_fails_when_db_ping_result_errors(client):
    class BrokenResult:
        def scalar_one(self):
            raise SQLAlchemyError("cursor failed")

    class BrokenSession:
        def execute(self, _stmt):
            return BrokenResult()

    def broken_db():
        yield BrokenSession()

    app.dependency_overrides[db] = broken_db
    try:
        resp = client.get("/health")
    finally:
        app.dependency_overrides.pop(db, None)

    assert resp.status_code == 503
    assert resp.json()["detail"] == "database unavailable"


def test_qa_eval_case_trims_name_and_preserves_valid_job_link(client, session):
    job_resp = client.post("/jobs", json={"name": "source-job"})
    assert job_resp.status_code == 201, job_resp.text
    job = job_resp.json()

    created = client.post(
        "/eval-cases",
        json={
            "name": "  source-case  ",
            "job_id": job["id"],
            "payload": {"fail_at": 0},
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "source-case"
    assert body["job_id"] == job["id"]

    session.expire_all()
    stored = session.get(EvalCase, body["id"])
    assert stored is not None
    assert stored.name == "source-case"
    assert stored.job_id == job["id"]

    duplicate = client.post(
        "/eval-cases",
        json={"name": "source-case", "job_id": job["id"]},
    )
    assert duplicate.status_code == 409, duplicate.text
    assert len(session.scalars(select(EvalCase)).all()) == 1


def test_qa_eval_case_patch_partial_update_preserves_unspecified_fields(
    client, session
):
    created = client.post(
        "/eval-cases",
        json={
            "name": "partial-regression",
            "payload": {"fail_at": 2, "nested": {"step": "review"}},
            "min_score": 0.8,
        },
    )
    assert created.status_code == 201, created.text
    case = created.json()

    resp = client.patch(f"/eval-cases/{case['id']}", json={"min_score": 0.4})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "partial-regression"
    assert body["engine"] == case["engine"]
    assert body["payload"] == {"fail_at": 2, "nested": {"step": "review"}}
    assert body["min_score"] == 0.4
    assert body["enabled"] is True

    session.expire_all()
    stored = session.get(EvalCase, case["id"])
    assert stored is not None
    assert stored.payload == {"fail_at": 2, "nested": {"step": "review"}}
    assert stored.min_score == 0.4
    assert stored.enabled is True


def test_qa_hosted_eval_case_rejects_foreign_job_id_without_persisting(
    client, qa_hosted, session
):
    auth_a = _mint_tenant(client, "team-a")
    auth_b = _mint_tenant(client, "team-b")
    job_b = client.post("/jobs", json={"name": "foreign-source"}, headers=auth_b).json()

    resp = client.post(
        "/eval-cases",
        json={"name": "cross-tenant", "job_id": job_b["id"]},
        headers=auth_a,
    )

    assert resp.status_code == 404, resp.text
    assert (
        session.scalars(select(EvalCase).where(EvalCase.name == "cross-tenant")).all()
        == []
    )


def test_qa_hosted_eval_case_patch_name_conflicts_are_tenant_scoped(
    client, qa_hosted
):
    auth_a = _mint_tenant(client, "team-a")
    auth_b = _mint_tenant(client, "team-b")
    job_a1 = client.post("/jobs", json={"name": "team-a-one"}, headers=auth_a).json()
    job_a2 = client.post("/jobs", json={"name": "team-a-two"}, headers=auth_a).json()
    job_b = client.post("/jobs", json={"name": "team-b-one"}, headers=auth_b).json()
    case_a1 = client.post(
        "/eval-cases",
        json={"name": "alpha", "job_id": job_a1["id"]},
        headers=auth_a,
    ).json()
    case_a2 = client.post(
        "/eval-cases",
        json={"name": "beta", "job_id": job_a2["id"]},
        headers=auth_a,
    ).json()
    client.post(
        "/eval-cases",
        json={"name": "shared", "job_id": job_b["id"]},
        headers=auth_b,
    )

    cross_tenant_same_name = client.patch(
        f"/eval-cases/{case_a1['id']}",
        json={"name": " shared "},
        headers=auth_a,
    )
    assert cross_tenant_same_name.status_code == 200, cross_tenant_same_name.text
    assert cross_tenant_same_name.json()["name"] == "shared"

    same_tenant_duplicate = client.patch(
        f"/eval-cases/{case_a2['id']}",
        json={"name": "shared"},
        headers=auth_a,
    )
    assert same_tenant_duplicate.status_code == 409, same_tenant_duplicate.text
