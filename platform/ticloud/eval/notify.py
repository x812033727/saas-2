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
from ..models import Alert

log = logging.getLogger(__name__)


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
    _push_webhook(alert)
    return alert


def _push_webhook(alert: Alert) -> None:
    url = getattr(settings, "webhook_url", None)
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
