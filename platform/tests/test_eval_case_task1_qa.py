import pytest
from sqlalchemy import select

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
