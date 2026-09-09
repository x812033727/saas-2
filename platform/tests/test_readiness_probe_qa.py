import json

import pytest
from sqlalchemy.exc import SQLAlchemyError

from ticloud import smoke
from ticloud.api.main import app, db
from ticloud.config import settings


def test_ready_probe_stays_open_when_hosted_mode_requires_tenant_key(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "required")

    assert client.get("/jobs").status_code == 401
    response = client.get("/ready")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ready",
        "version": app.version,
        "database": "ok",
    }


def test_ready_probe_returns_stable_503_when_database_ping_fails(client):
    class BrokenSession:
        def execute(self, _stmt):
            raise SQLAlchemyError("database is down")

    def broken_db():
        yield BrokenSession()

    app.dependency_overrides[db] = broken_db
    try:
        response = client.get("/ready")
    finally:
        app.dependency_overrides.pop(db, None)

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}


def test_http_smoke_fails_fast_when_ready_probe_is_not_ready(monkeypatch):
    calls = []

    def fake_http_get(_base_url, path):
        calls.append(path)
        if path == "/health":
            return json.dumps({"status": "ok"})
        if path == "/ready":
            return json.dumps({"status": "ok"})
        raise AssertionError(f"HTTP smoke should have stopped before {path}")

    monkeypatch.setattr(smoke, "_http_get", fake_http_get)

    with pytest.raises(RuntimeError, match="HTTP ready returned"):
        smoke._check_http_surfaces("http://127.0.0.1:8000/", {"nightly-patrol"})

    assert calls == ["/health", "/ready"]


def test_http_smoke_reports_non_json_ready_response(monkeypatch):
    def fake_http_get(_base_url, path):
        if path == "/health":
            return json.dumps({"status": "ok"})
        if path == "/ready":
            return "ready"
        raise AssertionError(f"HTTP smoke should have stopped before {path}")

    monkeypatch.setattr(smoke, "_http_get", fake_http_get)

    with pytest.raises(RuntimeError, match="HTTP smoke /ready did not return JSON"):
        smoke._check_http_surfaces("http://127.0.0.1:8000/", {"nightly-patrol"})
