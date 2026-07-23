import pytest
from pydantic import ValidationError

from ticloud.api.schemas import JobCreate, JobUpdate, TenantUpdate
from ticloud.config import Settings, settings
from ticloud.validation import validate_webhook_url

from test_api import create_job


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example/alert",
        "http://localhost:8080/webhook",
    ],
)
def test_validate_webhook_url_accepts_http_urls(url):
    assert validate_webhook_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://hooks.example/alert",
        "not-a-url",
        "https://bad host.example/alert",
        "https:///alert",
        "https://:443/alert",
        "https://@/alert",
    ],
)
def test_validate_webhook_url_rejects_invalid_or_empty_host(url):
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_settings_rejects_webhook_url_with_empty_host(monkeypatch):
    monkeypatch.setenv("TICLOUD_WEBHOOK_URL", "https://:443/alert")

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "schema_factory",
    [
        lambda webhook_url: JobCreate(name="qa-webhook", webhook_url=webhook_url),
        lambda webhook_url: JobUpdate(webhook_url=webhook_url),
        lambda webhook_url: TenantUpdate(webhook_url=webhook_url),
    ],
)
def test_api_webhook_schemas_reject_empty_host(schema_factory):
    with pytest.raises(ValidationError):
        schema_factory("https://:443/alert")


def test_create_job_endpoint_rejects_empty_host_webhook(client):
    resp = client.post(
        "/jobs",
        json={
            "name": "qa-empty-host-create",
            "engine": "offline",
            "cron": "0 2 * * *",
            "webhook_url": "https://:443/alert",
        },
    )

    assert resp.status_code == 422, resp.text


def test_patch_job_endpoint_rejects_empty_host_webhook(client):
    job = create_job(client, name="qa-empty-host-patch")

    resp = client.patch(f"/jobs/{job['id']}", json={"webhook_url": "https://@/alert"})

    assert resp.status_code == 422, resp.text


def test_admin_tenant_endpoint_rejects_empty_host_webhook(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "qa-admin-secret")
    tenant = client.post(
        "/admin/tenants",
        json={"name": "qa-empty-host-tenant"},
        headers={"Authorization": "Bearer qa-admin-secret"},
    ).json()

    resp = client.patch(
        f"/admin/tenants/{tenant['id']}",
        json={"webhook_url": "https://:443/alert"},
        headers={"Authorization": "Bearer qa-admin-secret"},
    )

    assert resp.status_code == 422, resp.text
