import pytest

from ticloud.config import settings
from ticloud.models import EvalCase, Job, Run, RunStatus


ADMIN = {"Authorization": "Bearer admin-secret"}
SHARED_ERROR = "RuntimeError: external service failed for account 12345"


@pytest.fixture
def admin_mode(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")


@pytest.fixture
def hosted(monkeypatch, admin_mode):
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()
    key = client.post(
        f"/admin/tenants/{tenant['id']}/keys", json={"name": "qa"}, headers=ADMIN
    ).json()
    return tenant, {"Authorization": f"Bearer {key['secret']}"}


def _add_failed_job(session, job_id: str, name: str, tenant_id: str | None = None):
    job = Job(
        id=job_id,
        name=name,
        tenant_id=tenant_id,
        engine="offline",
        payload={"case": name},
    )
    session.add(job)
    session.flush()
    session.add(Run(job_id=job.id, status=RunStatus.FAILED, error=SHARED_ERROR))
    return job


def test_hosted_partial_failure_mode_coverage_stays_unpromoted(client, hosted, session):
    tenant, auth = _mint_tenant(client, "qa-team")
    job_a = _add_failed_job(session, "aaaaaaaa" + ("1" * 24), "a", tenant["id"])
    job_b = _add_failed_job(session, "bbbbbbbb" + ("2" * 24), "b", tenant["id"])
    session.commit()

    mode = client.get("/failure-modes", headers=auth).json()[0]
    session.add(
        EvalCase(
            name="regression-a-only",
            job_id=job_a.id,
            engine=job_a.engine,
            payload=job_a.payload,
            source_signature=mode["signature"],
        )
    )
    session.commit()

    listed = client.get("/failure-modes", headers=auth).json()
    unpromoted = client.get("/failure-modes?unpromoted_only=true", headers=auth).json()
    promoted = client.post(
        "/failure-modes/promote",
        json={"signature": mode["signature"]},
        headers=auth,
    )
    after = client.get("/failure-modes?unpromoted_only=true", headers=auth).json()

    assert listed[0]["promoted"] is False
    assert [m["signature"] for m in unpromoted] == [mode["signature"]]
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["job_id"] == job_b.id
    assert client.get("/failure-modes", headers=auth).json()[0]["promoted"] is True
    assert after == []


def test_self_host_global_eval_case_still_covers_failure_mode(client, session):
    _add_failed_job(session, "cccccccc" + ("3" * 24), "self-host-a")
    _add_failed_job(session, "dddddddd" + ("4" * 24), "self-host-b")
    session.commit()

    mode = client.get("/failure-modes").json()[0]
    session.add(
        EvalCase(
            name="legacy-global",
            engine="offline",
            payload={},
            source_signature=mode["signature"],
        )
    )
    session.commit()

    listed = client.get("/failure-modes").json()
    unpromoted = client.get("/failure-modes?unpromoted_only=true").json()
    blocked = client.post("/failure-modes/promote", json={"signature": mode["signature"]})

    assert listed[0]["promoted"] is True
    assert unpromoted == []
    assert blocked.status_code == 409
    assert "already exists" in blocked.json()["detail"]
