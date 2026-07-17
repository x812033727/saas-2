"""Tenant API-key auth for hosted mode, plus the admin bearer gate.

Key format: ``tck_<40 hex chars>``. Only the SHA-256 hex of the full secret
is stored (``ApiKey.key_hash``); the secret is returned once at creation.

Modes (settings.auth_mode):
- "off" (default): self-host single-tenant — data routes take no credentials
  and ``current_tenant`` resolves to None (no scoping). This keeps the
  zero-config quick start intact.
- "required": every data route must present ``Authorization: Bearer tck_…``
  for a live key; queries are scoped to that key's tenant.

The /admin surface (tenant + key management, cross-tenant usage) is guarded
by a static bearer token (settings.admin_token) in both modes, and is
disabled with 503 while no token is configured.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ApiKey, Tenant

KEY_PREFIX = "tck_"


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_hex(20)


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer ") :].strip() or None


def make_current_tenant(db_dependency):
    """Build the tenant dependency against the app's DB session dependency."""

    def current_tenant(
        authorization: str | None = Header(None),
        session: Session = Depends(db_dependency),
    ) -> Tenant | None:
        if settings.auth_mode != "required":
            return None
        secret = _bearer(authorization)
        if not secret or not secret.startswith(KEY_PREFIX):
            raise HTTPException(401, "tenant API key required (Authorization: Bearer tck_…)")
        key = session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(secret)))
        if key is None or key.revoked_at is not None:
            raise HTTPException(401, "invalid or revoked API key")
        key.last_used_at = datetime.now(timezone.utc)
        session.commit()
        return key.tenant

    return current_tenant


def require_admin(authorization: str | None = Header(None)) -> None:
    if not settings.admin_token:
        raise HTTPException(503, "admin surface disabled: set TICLOUD_ADMIN_TOKEN")
    supplied = _bearer(authorization) or ""
    if not hmac.compare_digest(supplied, settings.admin_token):
        raise HTTPException(401, "admin token required")
