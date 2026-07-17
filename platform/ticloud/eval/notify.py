"""Alert delivery: persist always, push to a webhook when configured.

TICLOUD_WEBHOOK_URL receives a JSON POST whose shape is Slack
incoming-webhook compatible ({"text": ...} plus structured fields).
Delivery failure is logged but never affects run/gate processing.
"""

import json
import logging
import urllib.request

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Alert, Job

log = logging.getLogger(__name__)


def _resolve_webhook_url(session: Session, job_id: str) -> str | None:
    """Per-job override → owning tenant's URL → global TICLOUD_WEBHOOK_URL,
    so each hosted tenant gets its own destination instead of one shared one."""
    job = session.get(Job, job_id)
    if job is not None:
        if job.webhook_url:
            return job.webhook_url
        if job.tenant is not None and job.tenant.webhook_url:
            return job.tenant.webhook_url
    return getattr(settings, "webhook_url", None)


def raise_alert(
    session: Session,
    job_id: str,
    kind: str,
    message: str,
    run_id: str | None = None,
) -> Alert:
    alert = Alert(job_id=job_id, run_id=run_id, kind=kind, message=message)
    session.add(alert)
    session.commit()
    _push_webhook(alert, _resolve_webhook_url(session, job_id))
    return alert


def _push_webhook(alert: Alert, url: str | None) -> None:
    if not url:
        return
    payload = {
        "text": f":rotating_light: ticloud [{alert.kind}] {alert.message}",
        "kind": alert.kind,
        "job_id": alert.job_id,
        "run_id": alert.run_id,
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:  # noqa: BLE001 - alerting must never break the worker
        log.warning("webhook delivery failed: %s", exc)
