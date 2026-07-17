"""Hosted-mode foundation: tenants, API keys, scoping, usage metering.

Default mode ("off") must stay exactly as before — the whole existing suite
runs unauthenticated. These tests flip settings.auth_mode/admin_token per
test via monkeypatch (both are read at request time).
"""

import pytest
from sqlalchemy import inspect, text

from test_worker import run_job_once
from ticloud.config import settings
from ticloud.db import engine, init_db
from ticloud.models import Job

ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def admin_mode(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")


@pytest.fixture
def hosted(monkeypatch, admin_mode):
    monkeypatch.setattr(settings, "auth_mode", "required")


def _mint_tenant(client, name):
    tenant = client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()
    key = client.post(
        f"/admin/tenants/{tenant['id']}/keys", json={"name": "ci"}, headers=ADMIN
    ).json()
    return tenant, key, {"Authorization": f"Bearer {key['secret']}"}


# --- admin surface -----------------------------------------------------------


def test_admin_disabled_without_token(client):
    assert client.get("/admin/tenants").status_code == 503


def test_admin_requires_matching_token(client, admin_mode):
    assert client.get("/admin/tenants").status_code == 401
    assert client.get("/admin/tenants", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/admin/tenants", headers=ADMIN).status_code == 200


def test_tenant_and_key_lifecycle(client, admin_mode):
    tenant, key, _ = _mint_tenant(client, "acme")
    assert key["secret"].startswith("tck_")
    assert key["prefix"] == key["secret"][:12]

    listed = client.get(f"/admin/tenants/{tenant['id']}/keys", headers=ADMIN).json()
    assert len(listed) == 1 and "secret" not in listed[0]

    revoked = client.post(f"/admin/keys/{key['id']}/revoke", headers=ADMIN).json()
    assert revoked["revoked_at"] is not None

    dup = client.post("/admin/tenants", json={"name": "acme"}, headers=ADMIN)
    assert dup.status_code == 409


# --- single-tenant ("off") mode stays open ----------------------------------


def test_off_mode_needs_no_key(client):
    assert client.get("/jobs").status_code == 200
    assert client.get("/usage").json()["tenant_id"] is None


# --- hosted ("required") mode ------------------------------------------------


def test_hosted_mode_requires_key(client, hosted):
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Bearer tck_bogus"}).status_code == 401
    # health stays open (probes don't carry credentials)
    assert client.get("/health").status_code == 200


def test_revoked_key_is_rejected(client, hosted):
    _, key, auth = _mint_tenant(client, "acme")
    assert client.get("/jobs", headers=auth).status_code == 200
    client.post(f"/admin/keys/{key['id']}/revoke", headers=ADMIN)
    assert client.get("/jobs", headers=auth).status_code == 401


def test_tenant_isolation_matrix(client, hosted):
    _, _, auth_a = _mint_tenant(client, "team-a")
    _, _, auth_b = _mint_tenant(client, "team-b")

    job = client.post(
        "/jobs",
        json={"name": "a-patrol", "payload": {"fail_at": 3}, "max_retries": 0},
        headers=auth_a,
    ).json()
    assert job["id"]
    run = client.post(f"/jobs/{job['id']}/trigger", headers=auth_a).json()

    # A sees its world; B sees nothing of it.
    assert [j["name"] for j in client.get("/jobs", headers=auth_a).json()] == ["a-patrol"]
    assert client.get("/jobs", headers=auth_b).json() == []
    assert client.get(f"/jobs/{job['id']}", headers=auth_b).status_code == 404
    assert client.get(f"/jobs/{job['id']}/runs", headers=auth_b).status_code == 404
    assert client.get(f"/runs/{run['id']}", headers=auth_b).status_code == 404
    assert client.post(f"/jobs/{job['id']}/trigger", headers=auth_b).status_code == 404
    assert client.delete(f"/jobs/{job['id']}", headers=auth_b).status_code == 404
    assert client.get("/overview", headers=auth_b).json() == []


def test_hosted_job_ownership_and_failure_scoping(client, hosted, session):
    tenant_a, _, auth_a = _mint_tenant(client, "team-a")
    _, _, auth_b = _mint_tenant(client, "team-b")

    job = client.post(
        "/jobs",
        json={"name": "a-flaky", "payload": {"fail_at": 2}, "max_retries": 0},
        headers=auth_a,
    ).json()
    assert session.get(Job, job["id"]).tenant_id == tenant_a["id"]

    # Execute the queued run through the real worker so a failure lands.
    client.post(f"/jobs/{job['id']}/trigger", headers=auth_a)
    from ticloud.scheduler.queue import claim_next_run
    from ticloud.scheduler.worker import execute_run

    execute_run(claim_next_run(session).id)

    modes_a = client.get("/failure-modes", headers=auth_a).json()
    assert len(modes_a) == 1
    assert client.get("/failure-modes", headers=auth_b).json() == []
    alerts_a = client.get("/alerts", headers=auth_a).json()
    assert alerts_a and all(a["job_id"] == job["id"] for a in alerts_a)
    assert client.get("/alerts", headers=auth_b).json() == []

    # Promote is scoped too: B can't see A's mode.
    sig = modes_a[0]["signature"]
    assert (
        client.post("/failure-modes/promote", json={"signature": sig}, headers=auth_b).status_code
        == 404
    )
    case = client.post("/failure-modes/promote", json={"signature": sig}, headers=auth_a).json()
    assert case["job_id"] == job["id"]
    assert [c["id"] for c in client.get("/eval-cases", headers=auth_a).json()] == [case["id"]]
    assert client.get("/eval-cases", headers=auth_b).json() == []
    assert client.delete(f"/eval-cases/{case['id']}", headers=auth_b).status_code == 404


def test_tenants_have_independent_name_namespaces(client, hosted, session):
    """Same job name across tenants; same failure signature promotes for both."""
    _, _, auth_a = _mint_tenant(client, "team-a")
    _, _, auth_b = _mint_tenant(client, "team-b")

    spec = {"name": "daily-sync", "payload": {"fail_at": 1}, "max_retries": 0}
    job_a = client.post("/jobs", json=spec, headers=auth_a)
    job_b = client.post("/jobs", json=spec, headers=auth_b)
    assert (job_a.status_code, job_b.status_code) == (201, 201)
    # ...but still unique within one tenant.
    assert client.post("/jobs", json=spec, headers=auth_a).status_code == 409

    from ticloud.scheduler.queue import claim_next_run
    from ticloud.scheduler.worker import execute_run

    for auth, job in ((auth_a, job_a.json()), (auth_b, job_b.json())):
        client.post(f"/jobs/{job['id']}/trigger", headers=auth)
        execute_run(claim_next_run(session).id)

    # Identical normalized signature on both sides — each tenant can promote.
    sig_a = client.get("/failure-modes", headers=auth_a).json()[0]["signature"]
    sig_b = client.get("/failure-modes", headers=auth_b).json()[0]["signature"]
    assert sig_a == sig_b
    ok_a = client.post("/failure-modes/promote", json={"signature": sig_a}, headers=auth_a)
    ok_b = client.post("/failure-modes/promote", json={"signature": sig_b}, headers=auth_b)
    assert (ok_a.status_code, ok_b.status_code) == (201, 201)
    assert ok_a.json()["name"] != ok_b.json()["name"]


def test_hosted_eval_case_requires_owned_job(client, hosted):
    _, _, auth_a = _mint_tenant(client, "team-a")
    r = client.post("/eval-cases", json={"name": "global-case"}, headers=auth_a)
    assert r.status_code == 422
    r = client.post(
        "/eval-cases", json={"name": "foreign", "job_id": "nope"}, headers=auth_a
    )
    assert r.status_code == 404


def test_usage_metering_per_tenant(client, hosted, session):
    tenant_a, _, auth_a = _mint_tenant(client, "team-a")
    _, _, auth_b = _mint_tenant(client, "team-b")

    job = client.post("/jobs", json={"name": "a-usage"}, headers=auth_a).json()
    client.post(f"/jobs/{job['id']}/trigger", headers=auth_a)
    from ticloud.scheduler.queue import claim_next_run
    from ticloud.scheduler.worker import execute_run

    execute_run(claim_next_run(session).id)

    usage_a = client.get("/usage", headers=auth_a).json()
    assert usage_a["tenant_id"] == tenant_a["id"]
    assert len(usage_a["months"]) == 1
    month = usage_a["months"][0]
    assert month["runs"] == 1 and month["succeeded"] == 1
    assert month["cost_usd"] > 0 and month["tokens_in"] > 0

    assert client.get("/usage", headers=auth_b).json()["months"] == []

    # Admin cross-tenant view includes both tenants.
    all_usage = client.get("/admin/usage", headers=ADMIN).json()
    by_tenant = {u["tenant_id"]: u for u in all_usage}
    assert by_tenant[tenant_a["id"]]["months"][0]["runs"] == 1


def test_unowned_jobs_show_in_admin_usage(client, admin_mode, session):
    run_job_once(session)  # legacy single-tenant job, tenant_id NULL
    all_usage = client.get("/admin/usage", headers=ADMIN).json()
    unowned = [u for u in all_usage if u["tenant_id"] is None]
    assert unowned and unowned[0]["months"][0]["runs"] >= 1


# --- additive micro-migration -------------------------------------------------


def test_init_db_backfills_tenant_id_column():
    # Rebuild `jobs` without tenant_id (SQLite refuses to DROP an FK column),
    # simulating a database created before the column existed.
    legacy_cols = ", ".join(
        c["name"] for c in inspect(engine).get_columns("jobs") if c["name"] != "tenant_id"
    )
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE jobs_legacy AS SELECT {legacy_cols} FROM jobs"))
        conn.execute(text("DROP TABLE jobs"))
        conn.execute(text("ALTER TABLE jobs_legacy RENAME TO jobs"))
    assert "tenant_id" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
    init_db()  # also proves idempotency on a fully-migrated schema
    assert "tenant_id" in {c["name"] for c in inspect(engine).get_columns("jobs")}
    init_db()


def test_worker_runs_tenant_jobs(client, hosted, session, monkeypatch):
    """The worker is tenant-agnostic: owned jobs execute like any other."""
    monkeypatch.setattr(settings, "auth_mode", "required")
    _, _, auth_a = _mint_tenant(client, "team-a")
    job = client.post("/jobs", json={"name": "a-run"}, headers=auth_a).json()
    client.post(f"/jobs/{job['id']}/trigger", headers=auth_a)

    from ticloud.scheduler.queue import claim_next_run
    from ticloud.scheduler.worker import execute_run

    execute_run(claim_next_run(session).id)
    runs = client.get(f"/jobs/{job['id']}/runs", headers=auth_a).json()
    assert runs[0]["status"] == "succeeded"
