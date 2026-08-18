import json

import pytest

from ticloud import smoke
from ticloud.smoke import _normalize_base_url, _require_explicit_database_url, run


def test_launch_smoke_requires_explicit_database_url(monkeypatch):
    monkeypatch.delenv("TICLOUD_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="TICLOUD_DATABASE_URL"):
        _require_explicit_database_url()


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite:///./ticloud.db",
        " sqlite:///./ticloud.db ",
        "sqlite:///ticloud.db",
    ],
)
def test_launch_smoke_rejects_default_database_url_variants(monkeypatch, database_url):
    monkeypatch.setenv("TICLOUD_DATABASE_URL", database_url)

    with pytest.raises(RuntimeError, match="default ticloud.db"):
        _require_explicit_database_url()


def test_launch_smoke_cli_returns_failure_for_configuration_errors(monkeypatch, capsys):
    monkeypatch.delenv("TICLOUD_DATABASE_URL", raising=False)

    assert smoke.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "smoke FAIL:" in captured.err
    assert "TICLOUD_DATABASE_URL" in captured.err


def test_launch_smoke_seeds_demo_and_checks_operational_surfaces():
    summary = run()

    assert summary.health_status == "ok"
    assert summary.jobs == 3
    assert summary.alerts > 0
    assert summary.lessons > 0
    assert summary.http_base_url is None


def test_launch_smoke_can_check_running_http_surfaces(monkeypatch):
    calls = []

    def fake_http_get(base_url, path):
        calls.append((base_url, path))
        if path == "/health":
            return json.dumps({"status": "ok"})
        if path == "/templates":
            return json.dumps([{"id": "demo-workshop"}])
        if path == "/overview":
            return json.dumps(
                [
                    {"name": "nightly-patrol"},
                    {"name": "dep-upgrade"},
                    {"name": "long-audit"},
                ]
            )
        if path == "/metrics":
            return "\n".join(
                [
                    "ticloud_runs_total 1",
                    "ticloud_jobs 3",
                    "ticloud_alerts_unacknowledged 1",
                ]
            )
        raise AssertionError(path)

    monkeypatch.setattr(smoke, "_http_get", fake_http_get)

    summary = run("http://127.0.0.1:8000")

    assert summary.http_base_url == "http://127.0.0.1:8000/"
    assert calls == [
        ("http://127.0.0.1:8000/", "/health"),
        ("http://127.0.0.1:8000/", "/templates"),
        ("http://127.0.0.1:8000/", "/overview"),
        ("http://127.0.0.1:8000/", "/metrics"),
    ]


def test_launch_smoke_base_url_can_come_from_env(monkeypatch):
    monkeypatch.setenv("TICLOUD_SMOKE_BASE_URL", " http://api.local:8000/ ")

    assert _normalize_base_url() == "http://api.local:8000/"
