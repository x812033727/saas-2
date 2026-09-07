import json

import pytest
from pydantic import ValidationError

from ticloud import stripe_billing
from ticloud.config import Settings, settings
from ticloud.models import Tenant


ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def admin_mode(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    monkeypatch.setattr(settings, "stripe_webhook_secret", None)


def _tenant(client, name="qa-acme"):
    resp = client.post("/admin/tenants", json={"name": name}, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _event(etype, obj):
    return {"type": etype, "data": {"object": obj}}


def _post_stripe_event(client, event):
    return client.post(
        "/billing/stripe/webhook",
        content=json.dumps(event),
        headers={"content-type": "application/json"},
    )


def test_plan_budget_env_rejects_non_numeric_caps(monkeypatch):
    monkeypatch.setenv("TICLOUD_STRIPE_PLAN_BUDGETS", '{"free": "cheap"}')

    with pytest.raises(ValidationError):
        Settings()


def test_admin_plan_override_does_not_accept_removed_default_plan(
    client, admin_mode, session, monkeypatch
):
    monkeypatch.setattr(
        settings,
        "stripe_plan_budgets",
        {"free": 1.5, "growth": 25.0},
    )
    tenant = _tenant(client)

    resp = client.put(
        f"/admin/tenants/{tenant['id']}/plan",
        json={"plan": "team"},
        headers=ADMIN,
    )

    assert resp.status_code == 422
    assert "unknown plan" in resp.text

    row = session.get(Tenant, tenant["id"])
    assert row.plan == "free"
    assert row.subscription_status == "none"
    assert row.monthly_budget_usd is None


def test_stripe_unknown_plan_falls_back_to_configured_free_budget(
    client, admin_mode, session, monkeypatch
):
    monkeypatch.setattr(
        settings,
        "stripe_plan_budgets",
        {"free": 1.25, "growth": 25.0},
    )
    tenant = _tenant(client)

    resp = _post_stripe_event(
        client,
        _event(
            "checkout.session.completed",
            {
                "client_reference_id": tenant["id"],
                "customer": "cus_unknown",
                "metadata": {"plan": "enterprise"},
            },
        ),
    )

    row = session.get(Tenant, tenant["id"])
    assert resp.status_code == 200
    assert resp.json()["result"] == "activated:enterprise"
    assert row.plan == "enterprise"
    assert row.monthly_budget_usd == 1.25
    assert row.monthly_budget_usd != stripe_billing.PLAN_BUDGETS["free"]
