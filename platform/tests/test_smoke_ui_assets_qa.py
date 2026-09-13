import io
import json
import re
from urllib.error import HTTPError

import pytest

from ticloud import smoke


GOOD_SURFACES = {
    "/health": json.dumps({"status": "ok"}),
    "/ready": json.dumps({"status": "ready"}),
    "/ui/": '<title>Ti Cloud</title><main id="app"></main><script src="app.js"></script>',
    "/ui/app.js": "/* Ti Cloud dashboard */\nasync function api() {}\nasync function render() {}",
    "/ui/style.css": ":root {}\n.topbar {}\nmain {}",
    "/templates": json.dumps([{"id": "demo-workshop"}]),
    "/overview": json.dumps(
        [
            {"name": "nightly-patrol"},
            {"name": "dep-upgrade"},
            {"name": "long-audit"},
        ]
    ),
    "/metrics": "\n".join(
        [
            "ticloud_runs_total 1",
            "ticloud_jobs 3",
            "ticloud_alerts_unacknowledged 1",
        ]
    ),
}


@pytest.mark.parametrize(
    ("broken_path", "broken_body", "expected_message", "blocked_path"),
    [
        (
            "/ui/",
            '<title>Ti Cloud</title><main id="app"></main>',
            'HTTP dashboard missing src="app.js"',
            "/ui/app.js",
        ),
        (
            "/ui/style.css",
            ":root {}\nmain {}",
            "HTTP dashboard CSS missing .topbar",
            "/templates",
        ),
    ],
)
def test_http_smoke_fails_before_claiming_success_for_incomplete_ui_assets(
    monkeypatch, broken_path, broken_body, expected_message, blocked_path
):
    calls = []

    def fake_http_get(_base_url, path):
        calls.append(path)
        if path == broken_path:
            return broken_body
        return GOOD_SURFACES[path]

    monkeypatch.setattr(smoke, "_http_get", fake_http_get)

    with pytest.raises(RuntimeError, match=re.escape(expected_message)):
        smoke._check_http_surfaces("http://127.0.0.1:8000/", {"nightly-patrol"})

    assert blocked_path not in calls


def test_http_get_reports_dashboard_asset_http_status_and_body(monkeypatch):
    def fake_urlopen(request, timeout):
        raise HTTPError(
            request.full_url,
            404,
            "not found",
            hdrs=None,
            fp=io.BytesIO(b"asset missing"),
        )

    monkeypatch.setattr(smoke, "urlopen", fake_urlopen)

    with pytest.raises(
        RuntimeError,
        match=re.escape("HTTP smoke /ui/style.css returned 404: asset missing"),
    ):
        smoke._http_get("http://127.0.0.1:8000/", "/ui/style.css")
