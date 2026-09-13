from sqlalchemy import select

from ticloud.config import settings
from ticloud.models import EvalCase
from ticloud.scheduler.worker import execute_run


ADMIN = {"Authorization": "Bearer admin-secret"}


def _create_job(client, *, name, payload, headers=None):
    response = client.post(
        "/jobs",
        json={
            "name": name,
            "engine": "offline",
            "cron": None,
            "max_retries": 0,
            "payload": payload,
        },
        headers=headers or {},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _trigger_failed_run(client, job_id, *, headers=None):
    response = client.post(f"/jobs/{job_id}/trigger", headers=headers or {})
    assert response.status_code == 201, response.text
    execute_run(response.json()["id"])


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN)
    assert tenant.status_code == 201, tenant.text
    key = client.post(
        f"/admin/tenants/{tenant.json()['id']}/keys",
        json={"name": "qa-validation"},
        headers=ADMIN,
    )
    assert key.status_code == 201, key.text
    return {"Authorization": f"Bearer {key.json()['secret']}"}


def _enable_hosted_auth(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "auth_mode", "required")


def test_qa_unscoped_promote_requires_every_shared_job_before_hiding(client, session):
    jobs = [
        _create_job(client, name=f"qa-shared-{index}", payload={"fail_at": 4})
        for index in range(3)
    ]
    for job in jobs:
        _trigger_failed_run(client, job["id"])

    mode = client.get("/failure-modes").json()[0]
    signature = mode["signature"]
    expected_job_ids = {job["id"] for job in jobs}
    assert set(mode["job_ids"]) == expected_job_ids
    assert mode["promoted"] is False

    created_job_ids = set()
    for expected_created_count in range(1, 4):
        promoted = client.post("/failure-modes/promote", json={"signature": signature})
        assert promoted.status_code == 201, promoted.text
        created_job_ids.add(promoted.json()["job_id"])

        current_mode = client.get("/failure-modes").json()[0]
        if expected_created_count < 3:
            assert current_mode["promoted"] is False
            unpromoted = client.get("/failure-modes?unpromoted_only=true").json()
            assert [m["signature"] for m in unpromoted] == [signature]
        else:
            assert current_mode["promoted"] is True
            assert client.get("/failure-modes?unpromoted_only=true").json() == []

    assert created_job_ids == expected_job_ids
    cases = session.scalars(
        select(EvalCase).where(EvalCase.source_signature == signature)
    ).all()
    assert {case.job_id for case in cases} == expected_job_ids

    duplicate = client.post("/failure-modes/promote", json={"signature": signature})
    assert duplicate.status_code == 409


def test_qa_unscoped_promote_advances_only_within_current_tenant(client, monkeypatch):
    _enable_hosted_auth(monkeypatch)
    auth_a = _mint_tenant(client, "qa-validation-a")
    auth_b = _mint_tenant(client, "qa-validation-b")

    jobs_a = [
        _create_job(
            client,
            name=f"qa-tenant-a-shared-{index}",
            payload={"fail_at": 2},
            headers=auth_a,
        )
        for index in range(2)
    ]
    job_b = _create_job(
        client,
        name="qa-tenant-b-shared",
        payload={"fail_at": 2},
        headers=auth_b,
    )

    for job in [*jobs_a, job_b]:
        headers = auth_a if job in jobs_a else auth_b
        _trigger_failed_run(client, job["id"], headers=headers)

    mode_a = client.get("/failure-modes", headers=auth_a).json()[0]
    mode_b = client.get("/failure-modes", headers=auth_b).json()[0]
    assert mode_a["signature"] == mode_b["signature"]
    assert set(mode_a["job_ids"]) == {job["id"] for job in jobs_a}
    assert mode_b["job_ids"] == [job_b["id"]]

    first_a = client.post(
        "/failure-modes/promote",
        json={"signature": mode_a["signature"]},
        headers=auth_a,
    )
    assert first_a.status_code == 201, first_a.text
    assert client.get("/failure-modes", headers=auth_a).json()[0]["promoted"] is False
    assert client.get("/failure-modes", headers=auth_b).json()[0]["promoted"] is False

    second_a = client.post(
        "/failure-modes/promote",
        json={"signature": mode_a["signature"]},
        headers=auth_a,
    )
    assert second_a.status_code == 201, second_a.text
    assert {first_a.json()["job_id"], second_a.json()["job_id"]} == {
        job["id"] for job in jobs_a
    }
    assert client.get("/failure-modes", headers=auth_a).json()[0]["promoted"] is True

    still_unpromoted_b = client.get(
        "/failure-modes?unpromoted_only=true",
        headers=auth_b,
    )
    assert still_unpromoted_b.status_code == 200, still_unpromoted_b.text
    assert [m["signature"] for m in still_unpromoted_b.json()] == [mode_b["signature"]]

    promoted_b = client.post(
        "/failure-modes/promote",
        json={"signature": mode_b["signature"]},
        headers=auth_b,
    )
    assert promoted_b.status_code == 201, promoted_b.text
    assert promoted_b.json()["job_id"] == job_b["id"]
