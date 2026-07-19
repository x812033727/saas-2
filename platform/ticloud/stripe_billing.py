"""Stripe billing: subscription webhooks drive each tenant's plan → quota.

This sits on top of the per-tenant spend cap (billing.py): a plan is just a
named monthly budget, and Stripe subscription events keep the tenant's
``plan`` / ``subscription_status`` / ``monthly_budget_usd`` in sync.

The Stripe SDK is optional (``pip install "platform[billing]"``). Without a
configured webhook secret, the endpoint parses events unverified — fine for
local testing, NOT for production (a real deployment must set
``TICLOUD_STRIPE_WEBHOOK_SECRET`` so signatures are checked).

Plan → monthly USD cap. These are **placeholders** — set real numbers to
match your pricing (PLAN.md sketches Free / Team ~$59 / Pro). ``None`` = no
cap (unlimited).
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Tenant

log = logging.getLogger(__name__)

# Placeholder budgets — tune to your pricing. free is a trial-sized cap so an
# unpaid tenant can try the product but can't run unbounded spend.
PLAN_BUDGETS: dict[str, float | None] = {
    "free": 5.0,
    "team": 200.0,
    "pro": None,  # unlimited
}
DEFAULT_PLAN = "free"

# Subscription statuses Stripe considers "the customer is paying".
_ACTIVE_STATUSES = {"active", "trialing"}


class SignatureError(Exception):
    """Raised when a Stripe webhook signature fails verification."""


def parse_event(payload: bytes, sig_header: str | None) -> dict:
    """Verify (if a secret is configured) and return the event as a dict.

    With TICLOUD_STRIPE_WEBHOOK_SECRET set, the signature is checked via the
    Stripe SDK and a bad signature raises SignatureError. Without a secret,
    the raw JSON is parsed unverified (local/dev only)."""
    secret = settings.stripe_webhook_secret
    if not secret:
        return json.loads(payload)
    try:
        import stripe
    except ImportError as e:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "TICLOUD_STRIPE_WEBHOOK_SECRET is set but the Stripe SDK is not "
            "installed; `pip install \"platform[billing]\"`."
        ) from e
    try:
        event = stripe.Webhook.construct_event(payload, sig_header or "", secret)
    except Exception as e:  # stripe raises SignatureVerificationError / ValueError
        raise SignatureError(str(e)) from e
    # StripeObject supports dict-style access; normalize to a plain dict.
    return dict(event)


def budget_for_plan(plan: str) -> float | None:
    return PLAN_BUDGETS.get(plan, PLAN_BUDGETS[DEFAULT_PLAN])


def apply_plan(tenant: Tenant, plan: str, status: str) -> None:
    """Set a tenant's plan, subscription status, and derived spend cap.

    An unknown plan name falls back to the default plan's budget but keeps
    the reported name, so a new Stripe price never silently grants unlimited
    spend."""
    tenant.plan = plan
    tenant.subscription_status = status
    tenant.monthly_budget_usd = budget_for_plan(plan)


def _plan_from_object(obj: dict) -> str:
    """Pull the plan name from an event object's metadata, else default.

    The app is expected to stamp `metadata.plan` on the Checkout session and
    subscription (a price→plan lookup is the alternative, left as config)."""
    meta = obj.get("metadata") or {}
    return meta.get("plan") or DEFAULT_PLAN


def _tenant_by_customer(session: Session, customer_id: str | None) -> Tenant | None:
    if not customer_id:
        return None
    return session.scalar(select(Tenant).where(Tenant.stripe_customer_id == customer_id))


def handle_event(session: Session, event: dict) -> str:
    """Apply a Stripe subscription event to the matching tenant.

    Returns a short status string for logging/tests. Unrecognised events and
    events for unknown tenants are ignored (return "ignored") so the endpoint
    stays idempotent and never 500s Stripe into retb loops."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}

    if etype == "checkout.session.completed":
        # The app sets client_reference_id = tenant id when creating checkout.
        tenant = session.get(Tenant, obj.get("client_reference_id") or "")
        if tenant is None:
            return "ignored"
        customer = obj.get("customer")
        if customer:
            tenant.stripe_customer_id = customer
        apply_plan(tenant, _plan_from_object(obj), "active")
        session.commit()
        return f"activated:{tenant.plan}"

    if etype == "customer.subscription.updated":
        tenant = _tenant_by_customer(session, obj.get("customer"))
        if tenant is None:
            return "ignored"
        status = obj.get("status")
        if status in _ACTIVE_STATUSES:
            apply_plan(tenant, _plan_from_object(obj), status)
        else:
            # past_due / unpaid / incomplete / missing -> hold at free-tier spend.
            apply_plan(tenant, DEFAULT_PLAN, status or "none")
        session.commit()
        return f"updated:{tenant.subscription_status}"

    if etype == "customer.subscription.deleted":
        tenant = _tenant_by_customer(session, obj.get("customer"))
        if tenant is None:
            return "ignored"
        apply_plan(tenant, DEFAULT_PLAN, "canceled")
        session.commit()
        return "canceled"

    return "ignored"
