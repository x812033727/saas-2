"""Stripe billing: subscription webhooks drive tenant plan → spend cap.

No real Stripe: with no webhook secret configured, the endpoint parses
events unverified, so tests post plain event JSON. Signature verification
(secret set) is covered by monkeypatching the parser boundary.
"""

import json

import pytest

from ticloud import stripe_billing
from ticloud.config import settings
from ticloud.models import Tenant

ADMIN = {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def admin_mode(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "admin-secret")
    # Ensure the unverified (no-secret) webhook path.
    monkeypatch.setattr(settings, "stripe_webhook_secret", None)


def _tenant(client, name="acme"):
    return client.post("/admin/tenants", json={"name": name}, headers=ADMIN).json()


def _event(etype, obj):
    return {"type": etype, "data": {"object": obj}}


def _post(client, event):
    return client.post(
        "/billing/stripe/webhook",
        content=json.dumps(event),
        headers={"content-type": "application/json"},
    )


# --- new tenant defaults -----------------------------------------------------


def test_new_tenant_starts_on_free_plan(client, admin_mode):
    t = _tenant(client)
    assert t["plan"] == "free"
    assert t["subscription_status"] == "none"
    assert t["monthly_budget_usd"] is None  # unset until a plan is applied


# --- checkout completes → plan applied ---------------------------------------


def test_checkout_completed_activates_plan_and_budget(client, admin_mode, session):
    t = _tenant(client)
    resp = _post(
        client,
        _event(
            "checkout.session.completed",
            {
                "client_reference_id": t["id"],
                "customer": "cus_123",
                "metadata": {"plan": "team"},
            },
        ),
    )
    assert resp.status_code == 200 and resp.json()["result"] == "activated:team"

    row = session.get(Tenant, t["id"])
    assert row.plan == "team"
    assert row.subscription_status == "active"
    assert row.monthly_budget_usd == stripe_billing.PLAN_BUDGETS["team"]
    assert row.stripe_customer_id == "cus_123"


def test_pro_plan_is_unlimited(client, admin_mode, session):
    t = _tenant(client)
    _post(
        client,
        _event(
            "checkout.session.completed",
            {"client_reference_id": t["id"], "customer": "c1", "metadata": {"plan": "pro"}},
        ),
    )
    assert session.get(Tenant, t["id"]).monthly_budget_usd is None


def test_unknown_plan_falls_back_to_default_budget_not_unlimited(client, admin_mode, session):
    t = _tenant(client)
    _post(
        client,
        _event(
            "checkout.session.completed",
            {"client_reference_id": t["id"], "customer": "c1", "metadata": {"plan": "enterprise-x"}},
        ),
    )
    row = session.get(Tenant, t["id"])
    assert row.plan == "enterprise-x"  # name preserved
    assert row.monthly_budget_usd == stripe_billing.PLAN_BUDGETS["free"]  # safe budget


# --- subscription lifecycle --------------------------------------------------


def test_subscription_past_due_holds_at_free(client, admin_mode, session):
    t = _tenant(client)
    _post(client, _event("checkout.session.completed",
                         {"client_reference_id": t["id"], "customer": "cus_x", "metadata": {"plan": "team"}}))
    _post(client, _event("customer.subscription.updated",
                         {"customer": "cus_x", "status": "past_due", "metadata": {"plan": "team"}}))
    row = session.get(Tenant, t["id"])
    assert row.subscription_status == "past_due"
    assert row.plan == "free"
    assert row.monthly_budget_usd == stripe_billing.PLAN_BUDGETS["free"]


def test_subscription_updated_without_status_holds_at_default_plan(client, admin_mode, session):
    t = _tenant(client)
    _post(client, _event("checkout.session.completed",
                         {"client_reference_id": t["id"], "customer": "cus_missing", "metadata": {"plan": "team"}}))
    resp = _post(client, _event("customer.subscription.updated",
                                {"customer": "cus_missing", "metadata": {"plan": "team"}}))
    assert resp.status_code == 200
    assert resp.json()["result"] == "updated:none"

    row = session.get(Tenant, t["id"])
    assert row.plan == stripe_billing.DEFAULT_PLAN
    assert row.subscription_status != "active"
    assert row.monthly_budget_usd == stripe_billing.PLAN_BUDGETS[stripe_billing.DEFAULT_PLAN]


def test_subscription_updated_active_applies_paid_plan(client, admin_mode, session):
    t = _tenant(client)
    _post(client, _event("checkout.session.completed",
                         {"client_reference_id": t["id"], "customer": "cus_active", "metadata": {"plan": "free"}}))
    resp = _post(client, _event("customer.subscription.updated",
                                {"customer": "cus_active", "status": "active", "metadata": {"plan": "team"}}))
    assert resp.status_code == 200
    assert resp.json()["result"] == "updated:active"

    row = session.get(Tenant, t["id"])
    assert row.plan == "team"
    assert row.subscription_status == "active"
    assert row.monthly_budget_usd == stripe_billing.PLAN_BUDGETS["team"]


def test_subscription_deleted_downgrades_to_free(client, admin_mode, session):
    t = _tenant(client)
    _post(client, _event("checkout.session.completed",
                         {"client_reference_id": t["id"], "customer": "cus_y", "metadata": {"plan": "pro"}}))
    resp = _post(client, _event("customer.subscription.deleted", {"customer": "cus_y"}))
    assert resp.json()["result"] == "canceled"
    row = session.get(Tenant, t["id"])
    assert row.plan == "free" and row.subscription_status == "canceled"
    assert row.monthly_budget_usd == stripe_billing.PLAN_BUDGETS["free"]


# --- idempotency / unknown tenants -------------------------------------------


def test_event_for_unknown_tenant_is_ignored(client, admin_mode):
    resp = _post(client, _event("checkout.session.completed",
                                {"client_reference_id": "nope", "customer": "c", "metadata": {"plan": "team"}}))
    assert resp.status_code == 200 and resp.json()["result"] == "ignored"


def test_unrecognised_event_ignored(client, admin_mode):
    resp = _post(client, _event("invoice.paid", {"customer": "c"}))
    assert resp.json()["result"] == "ignored"


def test_malformed_payload_400(client, admin_mode):
    resp = client.post(
        "/billing/stripe/webhook",
        content="not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


# --- signature verification boundary -----------------------------------------


def test_bad_signature_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")

    def boom(payload, sig):
        raise stripe_billing.SignatureError("bad sig")

    monkeypatch.setattr(stripe_billing, "parse_event", boom)
    resp = _post(client, _event("checkout.session.completed", {}))
    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"]


# --- admin manual plan (comp accounts / no Stripe) ---------------------------


def test_admin_set_plan_applies_budget(client, admin_mode, session):
    t = _tenant(client)
    resp = client.put(f"/admin/tenants/{t['id']}/plan", json={"plan": "team"}, headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"] == "team"
    assert body["monthly_budget_usd"] == stripe_billing.PLAN_BUDGETS["team"]

    bad = client.put(f"/admin/tenants/{t['id']}/plan", json={"plan": "nope"}, headers=ADMIN)
    assert bad.status_code == 422


def test_plan_budget_enforced_end_to_end(client, admin_mode, session, monkeypatch):
    """A plan's cap actually gates runs, via the existing quota path."""
    monkeypatch.setattr(settings, "auth_mode", "required")
    t = _tenant(client)
    key = client.post(f"/admin/tenants/{t['id']}/keys", json={"name": "ci"}, headers=ADMIN).json()
    auth = {"Authorization": f"Bearer {key['secret']}"}

    # Put the tenant on free ($5 cap) and spend past it.
    client.put(f"/admin/tenants/{t['id']}/plan", json={"plan": "free"}, headers=ADMIN)
    job = client.post("/jobs", json={"name": "j"}, headers=auth).json()

    from datetime import datetime, timezone

    from ticloud.models import Run, RunStatus

    session.add(Run(job_id=job["id"], status=RunStatus.SUCCEEDED,
                    scheduled_at=datetime.now(timezone.utc),
                    started_at=datetime.now(timezone.utc), cost_usd=6.0))
    session.commit()
    assert client.post(f"/jobs/{job['id']}/trigger", headers=auth).status_code == 402
